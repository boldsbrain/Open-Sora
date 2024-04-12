from copy import deepcopy

import colossalai
import torch
import torch.nn as nn
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler
import torch.distributed as dist
import wandb
from colossalai.booster import Booster
from colossalai.booster.plugin import LowLevelZeroPlugin
from colossalai.cluster import DistCoordinator
from colossalai.nn.optimizer import HybridAdam
from colossalai.utils import get_current_device
from tqdm import tqdm
import os
from einops import rearrange

from opensora.acceleration.checkpoint import set_grad_checkpoint
from opensora.acceleration.parallel_states import (
    get_data_parallel_group,
    set_data_parallel_group,
    set_sequence_parallel_group,
)
from opensora.acceleration.plugin import ZeroSeqParallelPlugin
from opensora.datasets import DatasetFromCSV, get_transforms_image, get_transforms_video, prepare_dataloader
from opensora.registry import MODELS, SCHEDULERS, build_module
from opensora.utils.ckpt_utils import create_logger, load_json, save_json, load, model_sharding, record_model_param_shape, save
from opensora.utils.config_utils import (
    create_experiment_workspace,
    create_tensorboard_writer,
    parse_configs,
    save_training_config,
)
from opensora.utils.misc import all_reduce_mean, format_numel_str, get_model_numel, requires_grad, to_torch_dtype
from opensora.utils.train_utils import update_ema, MaskGenerator
from opensora.models.vae.vae_3d_v2 import VEALoss, DiscriminatorLoss, AdversarialLoss, pad_at_dim


def main():
    # ======================================================
    # 1. args & cfg
    # ======================================================
    cfg = parse_configs(training=True)
    print(cfg)
    exp_name, exp_dir = create_experiment_workspace(cfg)
    save_training_config(cfg._cfg_dict, exp_dir)

    # ======================================================
    # 2. runtime variables & colossalai launch
    # ======================================================
    assert torch.cuda.is_available(), "Training currently requires at least one GPU."
    assert cfg.dtype in ["fp16", "bf16"], f"Unknown mixed precision {cfg.dtype}"

    # 2.1. colossalai init distributed training
    colossalai.launch_from_torch({})
    coordinator = DistCoordinator()
    device = get_current_device()
    dtype = to_torch_dtype(cfg.dtype)

    # 2.2. init logger, tensorboard & wandb
    if not coordinator.is_master():
        logger = create_logger(None)
    else:
        logger = create_logger(exp_dir)
        logger.info(f"Experiment directory created at {exp_dir}")

        writer = create_tensorboard_writer(exp_dir)
        if cfg.wandb:
            wandb.init(project="opensora-vae", name=exp_name, config=cfg._cfg_dict)

    # 2.3. initialize ColossalAI booster
    if cfg.plugin == "zero2":
        plugin = LowLevelZeroPlugin(
            stage=2,
            precision=cfg.dtype,
            initial_scale=2**16,
            max_norm=cfg.grad_clip,
        )
        set_data_parallel_group(dist.group.WORLD)
    elif cfg.plugin == "zero2-seq":
        plugin = ZeroSeqParallelPlugin(
            sp_size=cfg.sp_size,
            stage=2,
            precision=cfg.dtype,
            initial_scale=2**16,
            max_norm=cfg.grad_clip,
        )
        set_sequence_parallel_group(plugin.sp_group)
        set_data_parallel_group(plugin.dp_group)
    else:
        raise ValueError(f"Unknown plugin {cfg.plugin}")
    booster = Booster(plugin=plugin)

    # ======================================================
    # 3. build dataset and dataloader
    # ======================================================
    dataset = DatasetFromCSV(
        cfg.data_path,
        # TODO: change transforms
        transform=(
            get_transforms_video(cfg.image_size[0])
            if not cfg.use_image_transform
            else get_transforms_image(cfg.image_size[0])
        ),
        num_frames=cfg.num_frames,
        frame_interval=cfg.frame_interval,
        root=cfg.root,
    )

    # TODO: use plugin's prepare dataloader
    # a batch contains:
    # {
    #      "video": torch.Tensor,  # [B, C, T, H, W],
    #      "text": List[str],
    # }
    dataloader = prepare_dataloader(
        dataset,
        batch_size=cfg.batch_size,
        num_workers=cfg.num_workers,
        shuffle=True,
        drop_last=True,
        pin_memory=True,
        process_group=get_data_parallel_group(),
    )
    logger.info(f"Dataset contains {len(dataset):,} videos ({cfg.data_path})")

    total_batch_size = cfg.batch_size * dist.get_world_size() // cfg.sp_size
    logger.info(f"Total batch size: {total_batch_size}")

    # ======================================================
    # 4. build model
    # ======================================================
    # 4.1. build model
    vae = build_module(cfg.model, MODELS, device=device)
    vae_numel, vae_numel_trainable = get_model_numel(vae)
    logger.info(
        f"Trainable vae params: {format_numel_str(vae_numel_trainable)}, Total model params: {format_numel_str(vae_numel)}"
    )
    
    discriminator = build_module(cfg.discriminator, MODELS, device=device)
    discriminator_numel, discriminator_numel_trainable = get_model_numel(discriminator)
    logger.info(
        f"Trainable discriminator params: {format_numel_str(discriminator_numel_trainable)}, Total model params: {format_numel_str(discriminator_numel)}"
    )

    # 4.3. move to device
    vae = vae.to(device, dtype)
    discriminator = discriminator.to(device, dtype)


    # 4.5. setup optimizer
    # vae optimizer
    optimizer = HybridAdam(
        filter(lambda p: p.requires_grad, vae.parameters()), lr=cfg.lr, weight_decay=0, adamw_mode=True
    )
    lr_scheduler = None
    # disc optimizer
    disc_optimizer = HybridAdam(
        filter(lambda p: p.requires_grad, discriminator.parameters()), lr=cfg.lr, weight_decay=0, adamw_mode=True
    )
    disc_lr_scheduler = None

    # 4.6. prepare for training
    if cfg.grad_checkpoint:
        set_grad_checkpoint(vae)
        set_grad_checkpoint(discriminator)
    vae.train()
    discriminator.train()


    # =======================================================
    # 5. boost model for distributed training with colossalai
    # =======================================================
    torch.set_default_dtype(dtype)
    vae, optimizer, _, dataloader, lr_scheduler = booster.boost(
        model=vae, optimizer=optimizer, lr_scheduler=lr_scheduler, dataloader=dataloader
    )
    torch.set_default_dtype(torch.float)
    num_steps_per_epoch = len(dataloader)
    logger.info("Boost vae for distributed training")


    discriminator, disc_optimizer, _, _, disc_lr_scheduler = booster.boost(
        model=discriminator, optimizer=disc_optimizer, lr_scheduler=disc_lr_scheduler
    )
    logger.info("Boost discriminator for distributed training")


    # =======================================================
    # 6. training loop
    # =======================================================
    start_epoch = start_step = log_step = sampler_start_idx = 0
    running_loss = 0.0
    running_disc_loss = 0.0


    # 6.1. resume training
    if cfg.load is not None:
        logger.info("Loading checkpoint")
        booster.load_model(vae, os.path.join(cfg.load, "model"))
        booster.load_model(discriminator, os.path.join(cfg.load, "discriminator"))
        booster.load_optimizer(optimizer, os.path.join(cfg.load, "optimizer"))
        booster.load_optimizer(disc_optimizer, os.path.join(cfg.load, "disc_optimizer"))
        if lr_scheduler is not None:
            booster.load_lr_scheduler(lr_scheduler, os.path.join(cfg.load, "lr_scheduler"))
        if disc_lr_scheduler is not None:
            booster.load_lr_scheduler(disc_lr_scheduler, os.path.join(cfg.load, "disc_lr_scheduler"))

        running_states = load_json(os.path.join(cfg.load, "running_states.json"))
        dist.barrier()
        start_epoch, start_step, sampler_start_idx = running_states["epoch"], running_states["step"], running_states["sample_start_index"]
        logger.info(f"Loaded checkpoint {cfg.load} at epoch {start_epoch} step {start_step}")
    logger.info(f"Training for {cfg.epochs} epochs with {num_steps_per_epoch} steps per epoch")

    dataloader.sampler.set_start_index(sampler_start_idx)

    # 6.2 Define loss functions
    nll_loss_fn = VEALoss(
        perceptual_loss_weight = cfg.perceptual_loss_weight,
        kl_loss_weight = cfg.kl_loss_weight,
        device=device,
        dtype=dtype,
    )

    adversarial_loss_fn = AdversarialLoss(
        discriminator_factor = cfg.discriminator_factor,
        discriminator_start = cfg.discriminator_start,
    )

    disc_loss_fn = DiscriminatorLoss(
        discriminator_factor = cfg.discriminator_factor,
        discriminator_start = cfg.discriminator_start,
        discriminator_loss = cfg.discriminator_loss,
    )   

    # 6.3. training loop

    # calculate discriminator_time_padding
    disc_time_downsample_factor = 2 ** len(cfg.discriminator.channel_multipliers)
    disc_time_padding = disc_time_downsample_factor - cfg.num_frames % disc_time_downsample_factor
    video_contains_first_frame = cfg.video_contains_first_frame

    for epoch in range(start_epoch, cfg.epochs):
        dataloader.sampler.set_epoch(epoch)
        dataloader_iter = iter(dataloader)
        logger.info(f"Beginning epoch {epoch}...")

        with tqdm(
            range(start_step, num_steps_per_epoch),
            desc=f"Epoch {epoch}",
            disable=not coordinator.is_master(),
            total=num_steps_per_epoch,
            initial=start_step,
        ) as pbar:
            for step in pbar:

                # SCH: calc global step at the start
                global_step = epoch * num_steps_per_epoch + step
            
                batch = next(dataloader_iter)
                x = batch["video"].to(device, dtype)  # [B, C, T, H, W]

                # supprt for image or video inputs
                assert x.ndim in {4, 5}, f"received input of {x.ndim} dimensions" # either image or video
                assert x.shape[-2:] == cfg.image_size, f"received input size {x.shape[-2:]}, but config image size is {cfg.image_size}"
                is_image = x.ndim == 4
                if is_image:
                    video = rearrange(x, 'b c ... -> b c 1 ...')
                    video_contains_first_frame = True
                else:
                    video = x

                # padded videos for GAN
                if global_step > cfg.discriminator_start:
                    real_video = pad_at_dim(video, (disc_time_padding, 0), value = 0., dim = 2)
                    fake_video = pad_at_dim(recon_video, (disc_time_padding, 0), value = 0., dim = 2)

                #  ====== VAE ======
                optimizer.zero_grad()
                recon_video, posterior = vae(
                    video,
                    video_contains_first_frame = video_contains_first_frame,
                )
                # simple nll loss
                nll_loss, nll_loss_log = nll_loss_fn(
                    video,
                    recon_video,
                    posterior,
                    split = "train"
                )
                vae_loss = nll_loss
                # adversarial loss 
                if global_step > cfg.discriminator_start:
                    fake_logits = discriminator(fake_video.contiguous)
                    adversarial_loss = adversarial_loss_fn(
                        fake_logits,
                        nll_loss, 
                        vae.get_last_layer(),
                        global_step,
                        is_training = vae.training,
                    )
                    vae_loss += adversarial_loss
                # Backward & update
                booster.backward(loss=vae_loss, optimizer=optimizer)
                optimizer.step()
                # Log loss values:
                all_reduce_mean(vae_loss)
                running_loss += vae_loss.item()
                
                #  ====== Discriminator ======
                if global_step > cfg.discriminator_start:
                    disc_optimizer.zero_grad()
                    # if video_contains_first_frame:
                    # Since we don't have enough T frames, pad anyways
                    real_logits = discriminator(real_video.contiguous.detach())
                    fake_logits = discriminator(fake_video.contiguous.detach())
                    disc_loss = disc_loss_fn(real_logits, fake_logits, global_step)
                    # Backward & update
                    booster.backward(loss=disc_loss, optimizer=disc_optimizer)
                    disc_optimizer.step()
                    # Log loss values:
                    all_reduce_mean(disc_loss)
                    running_disc_loss += disc_loss.item()
            
                log_step += 1

                # Log to tensorboard
                if coordinator.is_master() and (global_step + 1) % cfg.log_every == 0:
                    avg_loss = running_loss / log_step
                    avg_disc_loss = running_disc_loss / log_step
                    pbar.set_postfix({"loss": avg_loss, "disc_loss": avg_disc_loss, "step": step, "global_step": global_step})
                    running_loss = 0
                    log_step = 0
                    writer.add_scalar("loss", vae_loss.item(), global_step)
                    if cfg.wandb:
                        wandb.log(
                            {
                                "iter": global_step,
                                "num_samples": global_step * total_batch_size,
                                "epoch": epoch,
                                "loss": vae_loss.item(),
                                "disc_loss": disc_loss.item(),
                                "avg_loss": avg_loss,
                            },
                            step=global_step,
                        )

                # Save checkpoint
                if cfg.ckpt_every > 0 and (global_step + 1) % cfg.ckpt_every == 0:
                    save_dir = os.path.join(exp_dir, f"epoch{epoch}-global_step{global_step+1}")
                    os.makedirs(os.path.join(save_dir, "model"), exist_ok=True)
                    booster.save_model(vae, os.path.join(save_dir, "model"), shard=True)
                    booster.save_model(discriminator, os.path.join(save_dir, "discriminator"), shard=True)
                    booster.save_optimizer(optimizer, os.path.join(save_dir, "optimizer"), shard=True, size_per_shard=4096)
                    booster.save_optimizer(disc_optimizer, os.path.join(save_dir, "disc_optimizer"), shard=True, size_per_shard=4096)

                    if lr_scheduler is not None:
                        booster.save_lr_scheduler(lr_scheduler, os.path.join(save_dir, "lr_scheduler"))
                    if disc_lr_scheduler is not None:
                        booster.save_lr_scheduler(disc_lr_scheduler, os.path.join(save_dir, "disc_lr_scheduler"))

                    running_states = {
                        "epoch": epoch,
                        "step": step+1,
                        "global_step": global_step+1,
                        "sample_start_index": (step+1) * cfg.batch_size,
                    }
                    if coordinator.is_master():
                        save_json(running_states, os.path.join(save_dir, "running_states.json"))
                    dist.barrier()
                    logger.info(
                        f"Saved checkpoint at epoch {epoch} step {step + 1} global_step {global_step + 1} to {exp_dir}"
                    )

        # the continue epochs are not resumed, so we need to reset the sampler start index and start step
        dataloader.sampler.set_start_index(0)
        start_step = 0

if __name__ == "__main__":
    main()
