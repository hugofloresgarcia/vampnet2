import os
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import argbind
import torch
from audiotools import AudioSignal
from audiotools import ml
from audiotools.core import util
from audiotools.data import transforms
from audiotools.data.datasets import AudioDataset
from audiotools.data.datasets import AudioLoader
from audiotools.data.datasets import ConcatDataset
from audiotools.ml.decorators import timer
from audiotools.ml.decorators import Tracker
from audiotools.ml.decorators import when
from torch.utils.tensorboard import SummaryWriter

import soundmaterial as sm

import vampnet.dac as dac

warnings.filterwarnings("ignore", category=UserWarning)

# Enable cudnn autotuner to speed up training
# (can be altered by the funcs.seed function)
torch.backends.cudnn.benchmark = bool(int(os.getenv("CUDNN_BENCHMARK", 1)))
# Uncomment to trade memory for speed.

# Optimizers
AdamW = argbind.bind(torch.optim.AdamW, "generator", "discriminator")
Accelerator = argbind.bind(ml.Accelerator, without_prefix=True)


@argbind.bind("generator", "discriminator")
def ExponentialLR(optimizer, gamma: float = 1.0):
    return torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma)


# Models
DDSPDAC = argbind.bind(dac.model.DDSPDAC)

# Data
Dataset = argbind.bind(sm.dataset.Dataset, "train", "val")

# Transforms
filter_fn = lambda fn: hasattr(fn, "transform") and fn.__qualname__ not in [
    "BaseTransform",
    "Compose",
    "Choose",
]
tfm = argbind.bind_module(transforms, "train", "val", filter_fn=filter_fn)

# Loss
filter_fn = lambda fn: hasattr(fn, "forward") and "Loss" in fn.__name__
losses = argbind.bind_module(dac.nn.loss, filter_fn=filter_fn)


def get_infinite_loader(dataloader):
    while True:
        for batch in dataloader:
            yield batch


@argbind.bind
def build_transform(
    augment_prob: float = 1.0,
    preprocess: list = ["Identity"],
    augment: list = ["Identity"],
    postprocess: list = ["Identity"],
):
    to_tfm = lambda l: [getattr(tfm, x)() for x in l]
    preprocess = transforms.Compose(*to_tfm(preprocess), name="preprocess")
    augment = transforms.Compose(*to_tfm(augment), name="augment", prob=augment_prob)
    postprocess = transforms.Compose(*to_tfm(postprocess), name="postprocess")
    transform = transforms.Compose(preprocess, augment, postprocess)
    return transform

@argbind.bind
def build_datasets(
    sample_rate: int,
    db_path: str = "sm.db", 
    query: str = "SELECT * from audio_file", 
):
    # Give one loader per key/value of dictionary, where
    # value is a list of folders. Create a dataset for each one.
    # Concatenate the datasets with ConcatDataset, which
    # cycles through them.

    # datasets = []
    # for _, v in folders.items():
    #     loader = AudioLoader(sources=v)
    #     transform = build_transform()
    #     dataset = AudioDataset(loader, sample_rate, transform=transform)
    #     datasets.append(dataset)
    
    train_tfm = build_transform(augment_prob=1.0)    
    val_tfm = build_transform(augment_prob=1.0)   

    # dataset = ConcatDataset(datasets)
    # dataset.transform = transform
    import pandas as pd
    conn = sm.connect(db_path)
    print(f"loading data from {db_path}")
    df = pd.read_sql(query, conn)
    tdf, vdf = sm.dataset.train_test_split(df, test_size=0.1, seed=42)
    with argbind.scope(args, "train"):
        train_data = Dataset(
            tdf, sample_rate=sample_rate, transform=train_tfm
        )
    with argbind.scope(args, "val"):
        val_data = Dataset(
            vdf, sample_rate=sample_rate, transform=val_tfm
        )
    return train_data, val_data



@dataclass
class State:
    generator: DDSPDAC
    optimizer_g: AdamW
    scheduler_g: ExponentialLR

    stft_loss: losses.MultiScaleSTFTLoss
    mel_loss: losses.MelSpectrogramLoss
    # crepe_loss: losses.CREPELoss
    # clap_loss: losses.CLAPLoss

    train_data: AudioDataset
    val_data: AudioDataset

    tracker: Tracker


@argbind.bind(without_prefix=True)
def load(
    args,
    accel: ml.Accelerator,
    tracker: Tracker,
    save_path: str,
    encoder_ckpt: str = None,
    resume: bool = False,
    tag: str = "latest",
    load_weights: bool = True,
):
    generator, g_extra = None, {}

    if resume:
        assert load_weights, "Cannot resume without loading weights. use the --load_weights when launching "

    if resume:
        kwargs = {
            "folder": f"{save_path}/{tag}",
            "map_location": "cpu",
            "package": not load_weights,
        }
        tracker.print(f"Resuming from {str(Path('.').absolute())}/{kwargs['folder']}")
        pretrained = dac.model.DAC.load(encoder_ckpt)
        del pretrained.decoder # we don't need the old DAC decoder
        generator, g_extra = DDSPDAC.load_from_folder(pretrained, folder=kwargs['folder'], package=False, map_location="cpu")
    else:
        assert encoder_ckpt is not None
        if encoder_ckpt is not None:
            pretrained = dac.model.DAC.load(encoder_ckpt)
            del pretrained.decoder # we don't need the old DAC decoder
            generator = DDSPDAC(pretrained)

    generator.encoder.requires_grad_(False) # frozen encoder
    generator.decoder.requires_grad_(True) # unfrozen decoder

    tracker.print(generator)

    print("GENERATOR")
    print_model_parameters(generator.encoder)
    print_model_parameters(generator.decoder)

    # tracker.print(f"compiling...")
    # generator = torch.compile(generator)
    # tracker.print("finished compiling")

    generator = accel.prepare_model(generator)

    with argbind.scope(args, "generator"):
        optimizer_g = AdamW(generator.parameters(), use_zero=accel.use_ddp)
        scheduler_g = ExponentialLR(optimizer_g)

    if "optimizer.pth" in g_extra:
        optimizer_g.load_state_dict(g_extra["optimizer.pth"])
    if "scheduler.pth" in g_extra:
        scheduler_g.load_state_dict(g_extra["scheduler.pth"])
    if "tracker.pth" in g_extra:
        tracker.load_state_dict(g_extra["tracker.pth"])

    sample_rate = accel.unwrap(generator).sample_rate
    train_data, val_data = build_datasets(sample_rate)

    stft_loss = losses.MultiScaleSTFTLoss()
    mel_loss = losses.MelSpectrogramLoss()
    # clap_loss = losses.CLAPLoss()
    # crepe_loss = losses.CREPELoss()

    return State(
        generator=generator,
        optimizer_g=optimizer_g,
        scheduler_g=scheduler_g,
        stft_loss=stft_loss,
        mel_loss=mel_loss,
        # crepe_loss=crepe_loss,
        # clap_loss=clap_loss,
        tracker=tracker,
        train_data=train_data,
        val_data=val_data,
    )


@timer()
@torch.no_grad()
def val_loop(batch, state, accel):
    state.generator.eval()
    batch = util.prepare_batch(batch, accel.device)
    signal = state.val_data.transform(
        batch["signal"].clone(), **batch["transform_args"]
    )
    signal.samples = accel.unwrap(state.generator).preprocess(signal.samples, signal.sample_rate)

    out = state.generator(signal.audio_data, signal.sample_rate)
    recons = AudioSignal(out["audio"], signal.sample_rate)

    return {
        "loss": state.mel_loss(recons, signal),
        "mel/loss": state.mel_loss(recons, signal),
        "stft/loss": state.stft_loss(recons, signal),
        # "crepe/loss": state.crepe_loss(recons, signal),
        # "clap/loss": state.clap_loss(recons, signal),
        # "waveform/loss": state.waveform_loss(recons, signal),
    }


@timer()
def train_loop(state, batch, accel, lambdas):
    state.generator.train()
    output = {}

    batch = util.prepare_batch(batch, accel.device)
    with torch.no_grad():
        signal = state.train_data.transform(
            batch["signal"].clone(), **batch["transform_args"]
        )
        signal.samples = accel.unwrap(state.generator).preprocess(signal.samples, signal.sample_rate)

    with accel.autocast():
        out = state.generator(signal.audio_data, signal.sample_rate)
        recons = AudioSignal(out["audio"], signal.sample_rate)

    with accel.autocast():
        output["mel/loss"] = state.mel_loss(recons, signal)
        output["stft/loss"] = state.stft_loss(recons, signal)
        # output["crepe/loss"] = state.crepe_loss(recons, signal)
        # output["clap/loss"] = state.clap_loss(recons, signal)
        # TODO: add CLAP loss? we'll need to make sure the input signal long enough for the input CLAP window
        # output["waveform/loss"] = state.waveform_loss(recons, signal)
        output["loss"] = sum([v * output[k] for k, v in lambdas.items() if k in output])

    state.optimizer_g.zero_grad()
    accel.backward(output["loss"])
    accel.scaler.unscale_(state.optimizer_g)
    output["other/grad_norm"] = torch.nn.utils.clip_grad_norm_(
        state.generator.parameters(), 1e3
    )
    accel.step(state.optimizer_g)
    state.scheduler_g.step()
    accel.update()

    output["other/learning_rate"] = state.optimizer_g.param_groups[0]["lr"]
    output["other/batch_size"] = signal.batch_size * accel.world_size

    return {k: v for k, v in sorted(output.items())}


def checkpoint(state, save_iters, save_path):
    metadata = {"logs": state.tracker.history}

    tags = ["latest"]
    state.tracker.print(f"Saving to {str(Path('.').absolute())}")

    if state.tracker.is_best("val", "mel/loss"):
        state.tracker.print(f"Best generator so far")
        tags.append("best")

    if state.tracker.step in save_iters:
        tags.append(f"{state.tracker.step // 1000}k")

    for tag in tags:
        generator_extra = {
            "optimizer.pth": state.optimizer_g.state_dict(),
            "scheduler.pth": state.scheduler_g.state_dict(),
            "tracker.pth": state.tracker.state_dict(),
            "metadata.pth": metadata,
        }
        accel.unwrap(state.generator).metadata = metadata
        accel.unwrap(state.generator).save_to_folder(
            f"{save_path}/{tag}", generator_extra
        )


@torch.no_grad()
def save_samples(state, val_idx, writer):
    state.tracker.print("Saving audio samples to TensorBoard")
    state.generator.eval()

    samples = [state.val_data[idx] for idx in val_idx]
    batch = state.val_data.collate(samples)
    batch = util.prepare_batch(batch, accel.device)
    signal = state.train_data.transform(
        batch["signal"].clone(), **batch["transform_args"]
    )

    out = state.generator(signal.audio_data, signal.sample_rate)
    recons = AudioSignal(out["audio"], signal.sample_rate)

    audio_dict = {"recons": recons}
    if state.tracker.step == 0:
        audio_dict["signal"] = signal

    for k, v in audio_dict.items():
        for nb in range(v.batch_size):
            v[nb].cpu().write_audio_to_tb(
                f"{k}/sample_{nb}.wav", writer, state.tracker.step
            )


def validate(state, val_dataloader, accel):
    for batch in val_dataloader:
        output = val_loop(batch, state, accel)
    # Consolidate state dicts if using ZeroRedundancyOptimizer
    if hasattr(state.optimizer_g, "consolidate_state_dict"):
        state.optimizer_g.consolidate_state_dict()
        state.optimizer_d.consolidate_state_dict()
    return output


def print_model_parameters(model):
    """
    Prints the number of trainable and non-trainable parameters for each submodule in an nn.Module.

    Args:
        model (nn.Module): The PyTorch model to analyze.
    """
    print(f"Model: {model.__class__.__name__}")
    print("=" * 40)

    for name, submodule in model.named_children():
        total_params = sum(p.numel() for p in submodule.parameters())
        trainable_params = sum(p.numel() for p in submodule.parameters() if p.requires_grad)
        non_trainable_params = total_params - trainable_params

        print(f"Submodule: {name} ({submodule.__class__.__name__})")
        print(f"  Total parameters: {total_params:,}")
        print(f"  Trainable parameters: {trainable_params:,}")
        print(f"  Non-trainable parameters: {non_trainable_params:,}")
        print("-" * 40)


@argbind.bind(without_prefix=True)
def train(
    args,
    accel: ml.Accelerator,
    seed: int = 0,
    save_path: str = "ckpt",
    num_iters: int = 250000,
    save_iters: list = [10000, 50000, 100000, 200000],
    sample_freq: int = 10000,
    valid_freq: int = 1000,
    batch_size: int = 12,
    num_workers: int = 8,
    val_idx: list = [0, 1, 2, 3, 4, 5, 6, 7],
    lambdas: dict = {
        # "mel/loss": 1.0,
        "clap/loss": 1.0,
    },
):
    util.seed(seed)
    Path(save_path).mkdir(exist_ok=True, parents=True)
    writer = (
        SummaryWriter(log_dir=f"{save_path}/logs") if accel.local_rank == 0 else None
    )
    tracker = Tracker(
        writer=writer, log_file=f"{save_path}/log.txt", rank=accel.local_rank
    )

    val_batch_size = batch_size

    state = load(args, accel, tracker, save_path)

    train_dataloader = accel.prepare_dataloader(
        state.train_data,
        start_idx=state.tracker.step * batch_size,
        num_workers=num_workers,
        batch_size=batch_size,
        collate_fn=state.train_data.collate,
    )
    train_dataloader = get_infinite_loader(train_dataloader)
    val_dataloader = accel.prepare_dataloader(
        state.val_data,
        start_idx=0,
        num_workers=num_workers,
        batch_size=val_batch_size,
        collate_fn=state.val_data.collate,
        persistent_workers=True if num_workers > 0 else False,
    )

    # Wrap the functions so that they neatly track in TensorBoard + progress bars
    # and only run when specific conditions are met.
    global train_loop, val_loop, validate, save_samples, checkpoint
    train_loop = tracker.log("train", "value", history=False)(
        tracker.track("train", num_iters, completed=state.tracker.step)(train_loop)
    )
    val_loop = tracker.track("val", len(val_dataloader))(val_loop)
    validate = tracker.log("val", "mean")(validate)

    # These functions run only on the 0-rank process
    save_samples = when(lambda: accel.local_rank == 0)(save_samples)
    checkpoint = when(lambda: accel.local_rank == 0)(checkpoint)

    def dataload_time(t0):
        # print(f"took {time.time() - t0} to load data")
        return {"time": time.time() - t0}

    dataload_time = tracker.track("data", num_iters, completed=state.tracker.step)(dataload_time)
    dataload_time = tracker.log("data", "mean")(dataload_time)

    import time
    t0 = time.time()
    first_iter = tracker.step
    print("lets go!!")
    with tracker.live:
        for tracker.step, batch in enumerate(train_dataloader, start=tracker.step):
            # traclerprint(f"~"*50)
            # tracker.print(f"step: {tracker.step}")
            if tracker.step == first_iter:
                tracker.print("compiling... first step may take a while.")
            dataload_time(t0)
            train_loop(state, batch, accel, lambdas)

            last_iter = (
                tracker.step == num_iters - 1 if num_iters is not None else False
            )
            # first_iter = tracker.step == 0
            if tracker.step % sample_freq == 0 or last_iter:
                tracker.print(f"saving samples..")
                save_samples(state, val_idx, writer)
                tracker.print(f"done saving samples..")

            if (tracker.step % valid_freq == 0 or last_iter) and not first_iter:
                tracker.print(f"validating..")
                validate(state, val_dataloader, accel)
                tracker.print(f"done validating..")

                tracker.print("checkpointing..")
                checkpoint(state, save_iters, save_path)
                tracker.print("done checkpointing..")
                # Reset validation progress bar, print summary since last validation.
                tracker.done("val", f"Iteration {tracker.step}")

            if last_iter:
                break

            t0 = time.time()


if __name__ == "__main__":
    args = argbind.parse_args()
    args["args.debug"] = int(os.getenv("LOCAL_RANK", 0)) == 0
    with argbind.scope(args):
        with Accelerator() as accel:
            if accel.local_rank != 0:
                sys.tracebacklimit = 0
            train(args, accel)