import argparse
import logging
import torch
import torch.nn as nn
import torch.optim as optim
import wandb
import uuid
import json
from accelerate import Accelerator
from torch.optim.lr_scheduler import LambdaLR
from tqdm import tqdm
from torchmetrics.image.fid import FrechetInceptionDistance
from pathlib import Path


from aging_gan.utils import (
    set_seed,
    load_environ_vars,
    save_checkpoint,
    generate_and_save_samples,
    get_device,
)
from aging_gan.data import prepare_dataset
from aging_gan.model import initialize_models
from aging_gan.utils import terminate_ec2, archive_ec2

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for training and evaluation."""
    p = argparse.ArgumentParser()

    # hyperparams
    p.add_argument(
        "--gen_lr",
        type=float,
        default=2e-4,
        help="Initial learning rate for generators.",
    )
    p.add_argument(
        "--disc_lr",
        type=float,
        default=1e-4,
        help="Initial learning rate for discriminators.",
    )
    p.add_argument(
        "--num_train_epochs", type=int, default=100, help="Number of training epochs."
    )
    p.add_argument(
        "--train_batch_size",
        type=int,
        default=16,
        help="Batch size per device during training.",
    )
    p.add_argument(
        "--eval_batch_size",
        type=int,
        default=32,
        help="Batch size per device during evaluation.",
    )
    p.add_argument(
        "--lambda_adv_value",
        type=float,
        default=2.0,
        help="Weight for adversarial loss",
    )
    p.add_argument(
        "--lambda_cyc_value",
        type=float,
        default=4.0,
        help="Weight for cyclical loss",
    )
    p.add_argument(
        "--lambda_id_value",
        type=float,
        default=0.5,
        help="Weight for identity loss",
    )
    p.add_argument(
        "--weight_decay",
        type=float,
        default=1e-4,
    )

    # other params
    p.add_argument(
        "--set_seed",
        action="store_true",  # default=False
        help="Set seed for entire run for reproducibility.",
    )
    p.add_argument(
        "--seed", type=int, default=42, help="Random seed value for reproducibility."
    )
    p.add_argument(
        "--steps_for_logging_metrics",
        type=int,
        default=50,
        help="Print training metrics after certain batch steps.",
    )
    p.add_argument(
        "--num_sample_generations_to_save",
        type=int,
        default=10,
        help="The number of example generated images to save per epoch.",
    )
    p.add_argument(
        "--num_workers",
        type=int,
        default=3,
        help="Number of workers for dataloaders.",
    )
    p.add_argument(
        "--skip_test",
        action="store_false",
        dest="do_test",
        help="Skip evaluation on the test split after training.",
    )
    p.add_argument(
        "--archive_and_terminate_ec2",
        action="store_true",
        help="Upload outputs/ into s3 bucket and terminate current ec2.",
    )
    p.add_argument(
        "--s3_bucket_name",
        type=str,
        default="aging-gan",
        help="Name of your s3 bucket to sync outputs. Effective only when --archive_and_terminate_ec2.",
    )

    p.add_argument("--wandb_project", type=str, default="aging-gan")

    args = p.parse_args()
    return args


def initialize_optimizers(
    cfg, G, F, DX, DY
) -> tuple[optim.Optimizer, optim.Optimizer, optim.Optimizer, optim.Optimizer]:
    """Create Adam optimizers for all models."""
    opt_G = optim.Adam(
        G.parameters(),
        lr=cfg.gen_lr,
        betas=(0.5, 0.999),
        weight_decay=cfg.weight_decay,
    )
    opt_F = optim.Adam(
        F.parameters(),
        lr=cfg.gen_lr,
        betas=(0.5, 0.999),
        weight_decay=cfg.weight_decay,
    )
    opt_DX = optim.Adam(
        DX.parameters(),
        lr=cfg.disc_lr,
        betas=(0.5, 0.999),
        weight_decay=cfg.weight_decay,
    )
    opt_DY = optim.Adam(
        DY.parameters(),
        lr=cfg.disc_lr,
        betas=(0.5, 0.999),
        weight_decay=cfg.weight_decay,
    )

    return opt_G, opt_F, opt_DX, opt_DY


def initialize_loss_functions(
    lambda_adv_value: float = 2.0,
    lambda_cyc_value: float = 10.0,
    lambda_id_value: float = 7.0,
) -> tuple[nn.Module, nn.Module, float, float, float]:
    """Return basic CycleGAN loss functions and weights."""
    mse = nn.MSELoss()
    l1 = nn.L1Loss()
    lambda_adv = lambda_adv_value
    lambda_cyc = lambda_cyc_value
    lambda_id = lambda_id_value

    return mse, l1, lambda_adv, lambda_cyc, lambda_id


def make_schedulers(
    cfg, opt_G, opt_F, opt_DX, opt_DY
) -> tuple[LambdaLR, LambdaLR, LambdaLR, LambdaLR]:
    """Return LR schedulers that decay linearly after half the run."""
    # keep lr constant constant for the first half, then linearly decay to 0
    n_epochs = cfg.num_train_epochs
    start_decay = n_epochs // 2

    def _lr_lambda(epoch):
        if epoch < start_decay:
            return 1.0
        # linearly decay from 1.0 -> 0.0 from half of run
        return max(0.0, (n_epochs - epoch) / (n_epochs - start_decay))

    sched_G = LambdaLR(opt_G, lr_lambda=_lr_lambda)
    sched_F = LambdaLR(opt_F, lr_lambda=_lr_lambda)
    sched_DX = LambdaLR(opt_DX, lr_lambda=_lr_lambda)
    sched_DY = LambdaLR(opt_DY, lr_lambda=_lr_lambda)

    return sched_G, sched_F, sched_DX, sched_DY


def perform_train_step(
    G,
    F,  # generator models
    DX,
    DY,  # discriminator models
    real_data,
    mse,
    l1,
    lambda_adv,
    lambda_cyc,
    lambda_id,  # loss functions and loss params
    opt_G,
    opt_F,  # generator optimizers
    opt_DX,
    opt_DY,  # discriminator optimizers
    accelerator,
) -> dict[str, float]:
    """Run a single optimization step for generators and discriminators."""
    x, y = real_data
    # ------ Update Generators ------
    opt_G.zero_grad(set_to_none=True)
    opt_F.zero_grad(set_to_none=True)
    with accelerator.autocast():
        fake_x = F(y)
        fake_y = G(x)
        rec_x = F(fake_y)
        rec_y = G(fake_x)
        # Loss 1: adversarial terms
        fake_test_logits = DX(fake_x)  # fake x logits
        loss_f_adv = lambda_adv * mse(
            fake_test_logits, torch.ones_like(fake_test_logits)
        )
        fake_test_logits = DY(fake_y)  # fake y logits
        loss_g_adv = lambda_adv * mse(
            fake_test_logits, torch.ones_like(fake_test_logits)
        )
        # Loss 2: cycle terms
        loss_cyc = lambda_cyc * (l1(rec_x, x) + l1(rec_y, y))
        # Loss 3: identity terms
        loss_id = lambda_id * (l1(G(y), y) + l1(F(x), x))
        # Total loss
        loss_gen_total = loss_g_adv + loss_f_adv + loss_cyc + loss_id
    # Backprop + grad norm + step
    accelerator.backward(loss_gen_total)
    accelerator.clip_grad_norm_(
        list(G.parameters()) + list(F.parameters()), max_norm=1.0
    )
    opt_G.step()
    opt_F.step()

    # ------ Update Discriminators ------
    # DX: real young vs fake young
    opt_DX.zero_grad(set_to_none=True)
    with accelerator.autocast():
        real_logits = DX(x)
        real_loss = mse(real_logits, torch.ones_like(real_logits))
        fake_logits = DX(fake_x.detach())
        fake_loss = mse(fake_logits, torch.zeros_like(fake_logits))
        # DX loss
        loss_DX = 0.5 * (real_loss + fake_loss)
    # backprop + grad norm + step
    accelerator.backward(loss_DX)
    accelerator.clip_grad_norm_(DX.parameters(), max_norm=1.0)
    opt_DX.step()

    # DY: real old vs fake old
    opt_DY.zero_grad(set_to_none=True)
    with accelerator.autocast():
        real_logits = DY(y)
        real_loss = mse(real_logits, torch.ones_like(real_logits))
        fake_logits = DY(fake_y.detach())
        fake_loss = mse(fake_logits, torch.zeros_like(fake_logits))
        # DY loss
        loss_DY = 0.5 * (
            real_loss + fake_loss
        )  # average loss to prevent discriminator learning "too quickly" compread to generators.
    # backprop + grad norm + step
    accelerator.backward(loss_DY)
    accelerator.clip_grad_norm_(DY.parameters(), max_norm=1.0)
    opt_DY.step()

    return {
        "train/loss_DX": loss_DX.item(),
        "train/loss_DY": loss_DY.item(),
        "train/loss_f_adv": loss_f_adv.item(),
        "train/loss_g_adv": loss_g_adv.item(),
        "train/loss_cyc": loss_cyc.item(),
        "train/loss_id": loss_id.item(),
        "train/loss_gen_total": loss_gen_total.item(),
    }


def evaluate_epoch(
    G,
    F,  # generator models
    DX,
    DY,  # discriminator models
    loader,
    split: str,  # either "val" or "test"
    mse,
    l1,
    lambda_adv,
    lambda_cyc,
    lambda_id,  # loss functions and loss params
    fid_metric,
    accelerator,
) -> dict[str, float]:
    """Evaluate models on ``loader`` and return averaged metrics."""
    metrics = {
        f"{split}/loss_DX": 0.0,
        f"{split}/loss_DY": 0.0,
        f"{split}/loss_f_adv": 0.0,
        f"{split}/loss_g_adv": 0.0,
        f"{split}/loss_cyc": 0.0,
        f"{split}/loss_id": 0.0,
        f"{split}/loss_gen_total": 0.0,
        f"{split}/fid_val": 0.0,
    }
    n_batches = 0

    with torch.no_grad(), accelerator.autocast():
        fid_metric.reset()
        for x, y in tqdm(loader):
            # Forward: Generate fakes and reconstrucitons
            fake_x = F(y)
            fake_y = G(x)
            rec_x = F(fake_y)
            rec_y = G(fake_x)
            # ------ Evaluate Generators ------
            # Loss 1: adversarial terms
            fake_test_logits = DX(fake_x)  # fake x logits
            loss_f_adv = lambda_adv * mse(
                fake_test_logits, torch.ones_like(fake_test_logits)
            )

            fake_test_logits = DY(fake_y)  # fake y logits
            loss_g_adv = lambda_adv * mse(
                fake_test_logits, torch.ones_like(fake_test_logits)
            )
            # Loss 2: cycle terms
            loss_cyc = lambda_cyc * (l1(rec_x, x) + l1(rec_y, y))
            # Loss 3: identity terms
            loss_id = lambda_id * (l1(G(y), y) + l1(F(x), x))
            # Total loss
            loss_gen_total = loss_g_adv + loss_f_adv + loss_cyc + loss_id

            # ------ Evaluate Discriminators ------
            # DX: real young vs fake young
            real_logits = DX(x)
            real_loss = mse(real_logits, torch.ones_like(real_logits))

            fake_logits = DX(fake_x)
            fake_loss = mse(fake_logits, torch.zeros_like(fake_logits))
            # DX loss
            loss_DX = 0.5 * (real_loss + fake_loss)

            # DY: real old vs fake old
            real_logits = DY(y)
            real_loss = mse(real_logits, torch.ones_like(real_logits))

            fake_logits = DY(fake_y)
            fake_loss = mse(fake_logits, torch.zeros_like(fake_logits))

            # DY loss
            loss_DY = 0.5 * (
                real_loss + fake_loss
            )  # average loss to prevent discriminator learning "too quickly" compread to generators.

            # FID metric (normalize to range of [0,1] from [-1,1])
            # FID expects float32 images, which can raise dtype warning for mixed precision batches unless converted.
            fid_metric.update((y * 0.5 + 0.5).float(), real=True)
            fid_metric.update((fake_y * 0.5 + 0.5).float(), real=False)

            # ------ Accumulate ------
            metrics[f"{split}/loss_DX"] += loss_DX.item()
            metrics[f"{split}/loss_DY"] += loss_DY.item()
            metrics[f"{split}/loss_f_adv"] += loss_f_adv.item()
            metrics[f"{split}/loss_g_adv"] += loss_g_adv.item()
            metrics[f"{split}/loss_cyc"] += loss_cyc.item()
            metrics[f"{split}/loss_id"] += loss_id.item()
            metrics[f"{split}/loss_gen_total"] += loss_gen_total.item()

            n_batches += 1

        # Compute epoch fid metric
        fid_val = fid_metric.compute()
        metrics[f"{split}/fid_val"] = fid_val.item()

    # per-batch average
    for k in metrics:
        metrics[k] /= n_batches

    return metrics


def perform_epoch(
    cfg,
    train_loader,
    val_loader,
    G,
    F,
    DX,
    DY,
    mse,
    l1,
    lambda_adv,
    lambda_cyc,
    lambda_id,
    opt_G,
    opt_F,  # generator optimizers
    opt_DX,
    opt_DY,  # discriminator optimizers
    sched_G,
    sched_F,
    sched_DX,
    sched_DY,  # schedulers
    epoch,
    accelerator,
    fid_metric,
) -> dict[str, float]:
    """Perform a single epoch."""
    # TRAINING
    logger.info("Training...")
    G.train()
    F.train()
    DX.train()
    DY.train()
    batches_per_epoch = len(train_loader)
    for batch_no, real_data in enumerate(tqdm(train_loader)):
        train_metrics = perform_train_step(
            G,
            F,  # generator models
            DX,
            DY,  # discriminator models
            real_data,
            mse,
            l1,
            lambda_adv,
            lambda_cyc,
            lambda_id,  # loss functions and loss params
            opt_G,
            opt_F,  # generator optimizers
            opt_DX,
            opt_DY,  # discriminator optimizers
            accelerator,
        )
        # Print statistics and generate iamge after every n-th batch
        if batch_no % cfg.steps_for_logging_metrics == 0:
            epoch_float = epoch + (batch_no + 1) / batches_per_epoch
            logger.info(
                f"train/loss_DX: {train_metrics['train/loss_DX']:.4f} | train/loss_DY: {train_metrics['train/loss_DY']:.4f} | train/loss_gen_total: {train_metrics['train/loss_gen_total']:.4f} | train/loss_g_adv: {train_metrics['train/loss_g_adv']:.4f} | train/loss_f_adv: {train_metrics['train/loss_f_adv']:.4f} | train/loss_cyc: {train_metrics['train/loss_cyc']:.4f} | train/loss_id: {train_metrics['train/loss_id']:.4f}"
            )
            train_metrics["train/epoch_float"] = epoch_float
            wandb.log(train_metrics)
    # Step schedulers per epoch
    sched_G.step()
    sched_F.step()
    sched_DX.step()
    sched_DY.step()
    # log geneartor and discriminator LR
    wandb.log({"train/current_G_lr": sched_G.get_last_lr()[0]})
    wandb.log({"train/current_F_lr": sched_F.get_last_lr()[0]})
    wandb.log({"train/current_DX_lr": sched_DX.get_last_lr()[0]})
    wandb.log({"train/current_DY_lr": sched_DY.get_last_lr()[0]})

    # EVALUATION
    logger.info("Evlauating...")
    G.eval()
    F.eval()
    DX.eval()
    DY.eval()
    val_metrics = evaluate_epoch(
        G,
        F,  # generator models
        DX,
        DY,  # discriminator models
        val_loader,
        "val",
        mse,
        l1,
        lambda_adv,
        lambda_cyc,
        lambda_id,  # loss functions and loss params
        fid_metric,  # evaluation metric
        accelerator,
    )
    logger.info(
        f"val/loss_DX: {val_metrics['val/loss_DX']:.4f} | val/loss_DY: {val_metrics['val/loss_DY']:.4f} | val/fid_val: {val_metrics['val/fid_val']:.4f} | val/loss_gen_total: {val_metrics['val/loss_gen_total']:.4f} | val/loss_g_adv: {val_metrics['val/loss_g_adv']:.4f} | val/loss_f_adv: {val_metrics['val/loss_f_adv']:.4f} | val/loss_cyc: {val_metrics['val/loss_cyc']:.4f} | val/loss_id: {val_metrics['val/loss_id']:.4f}"
    )
    wandb.log(val_metrics)
    # save example generated images
    generate_and_save_samples(
        G,
        val_loader,
        epoch,
        get_device(),
        cfg.num_sample_generations_to_save,
    )
    # Clear memory after every epoch
    torch.cuda.empty_cache()

    return val_metrics


def main() -> None:
    """Entry point: parse args, prepare data, train, evaluate, and optionally test."""
    logging.basicConfig(level=logging.INFO)
    cfg = parse_args()
    load_environ_vars(cfg.wandb_project)

    # ---------- Run Initialization ----------
    # wandb
    run_name = uuid.uuid4().hex[:8]
    wandb.init(
        project=cfg.wandb_project,
        name=run_name,
        config={
            k: v for k, v in vars(cfg).items() if not k.startswith("_")
        },  # drop python's or "private" framework-internal attributes
    )
    wandb.define_metric(
        "train/epoch_float"
    )  # defaults main metric to epoch floats rather than steps
    wandb.define_metric("train/*", step_metric="train/epoch_float")
    wandb.define_metric("val/*", step_metric="train/epoch_float")
    wandb.define_metric("test/*", step_metric="train/epoch_float")
    # choose device
    logger.info(f"Using: {get_device()}")
    # reproducibility (optional, but less efficient if set)
    if cfg.set_seed:
        set_seed(cfg.seed)
        logger.info(f"Set seed: {cfg.seed}")
    else:
        logger.info("Skipping setting seed...")
    # speedups (Enable cuDNN auto-tuner which is good for fixed input shapes)
    torch.backends.cudnn.benchmark = True

    # ---------- Data Preprocessing ----------
    train_loader, val_loader, test_loader = prepare_dataset(
        cfg.train_batch_size,
        cfg.eval_batch_size,
        cfg.num_workers,
        seed=cfg.seed,
    )

    # ---------- Models, Optimizers, Loss Functions, Schedulers Initialization ----------
    # Initialize the generators (G, F) and discriminators (DX, DY)
    G, F, DX, DY = initialize_models()
    # Initialize optimizers
    (
        opt_G,
        opt_F,
        opt_DX,
        opt_DY,
    ) = initialize_optimizers(cfg, G, F, DX, DY)
    # Prepare Accelerator (uses hf accelerate to move models to correct device,
    # wrap in DDP if needed, shard the dataloader, and enable mixed-precision).
    accelerator = Accelerator(mixed_precision="fp16")
    (
        G,
        F,
        DX,
        DY,
        opt_G,
        opt_F,
        opt_DX,
        opt_DY,
        train_loader,
        val_loader,
        test_loader,
    ) = accelerator.prepare(
        G,
        F,
        DX,
        DY,
        opt_G,
        opt_F,
        opt_DX,
        opt_DY,
        train_loader,
        val_loader,
        test_loader,
    )
    # Loss functions and scalers
    mse, l1, lambda_adv, lambda_cyc, lambda_id = initialize_loss_functions(
        cfg.lambda_adv_value, cfg.lambda_cyc_value, cfg.lambda_id_value
    )
    # Initialize schedulers (It it important this comes AFTER wrapping optimizers in accelerator)
    sched_G, sched_F, sched_DX, sched_DY = make_schedulers(
        cfg, opt_G, opt_F, opt_DX, opt_DY
    )
    # Initialize FID metric for evaluation
    fid_metric = FrechetInceptionDistance(feature=2048, normalize=True).to(
        accelerator.device
    )

    # ---------- Train, Evaluate, & Checkpoint ----------
    best_fid = float("inf")  # keep track of the best FID score for each epoch
    for epoch in range(1, cfg.num_train_epochs + 1):
        logger.info(f"\nEPOCH {epoch}")
        val_metrics = perform_epoch(
            cfg,
            train_loader,
            val_loader,
            G,
            F,
            DX,
            DY,
            mse,
            l1,
            lambda_adv,
            lambda_cyc,
            lambda_id,
            opt_G,
            opt_F,  # generator optimizers
            opt_DX,
            opt_DY,  # discriminator optimizers
            sched_G,
            sched_F,
            sched_DX,
            sched_DY,  # schedulers
            epoch,
            accelerator,
            fid_metric,
        )
        # save the best models with the lowest fid score
        if val_metrics["val/fid_val"] < best_fid:
            best_fid = val_metrics["val/fid_val"]
            save_checkpoint(
                epoch,
                G,
                F,
                DX,
                DY,
                opt_G,
                opt_F,  # generator optimizers
                opt_DX,
                opt_DY,  # discriminator optimizers
                sched_G,
                sched_F,
                sched_DX,
                sched_DY,  # schedulers
                "best",
            )
            # upload outputs to s3 bucket
            if cfg.archive_and_terminate_ec2:
                archive_ec2(bucket=cfg.s3_bucket_name, prefix=f"outputs/run-{run_name}")
        # save the latest checkpoint
        if epoch % 5 == 0:
            save_checkpoint(
                epoch,
                G,
                F,
                DX,
                DY,
                opt_G,
                opt_F,  # generator optimizers
                opt_DX,
                opt_DY,  # discriminator optimizers
                sched_G,
                sched_F,
                sched_DX,
                sched_DY,  # schedulers
                "current",
            )
            # upload outputs to s3 bucket
            if cfg.archive_and_terminate_ec2:
                archive_ec2(bucket=cfg.s3_bucket_name, prefix=f"outputs/run-{run_name}")

    # ---------- Test ----------
    if cfg.do_test:
        logger.info("Running final test-set evaluation on best checkpoint...")
        best_ckpt_path = (
            Path(__file__).resolve().parents[2] / "outputs/checkpoints/best.pth"
        )
        best_ckpt = torch.load(best_ckpt_path, map_location=get_device())

        # load the best weights into each model
        # unwrap models first so that the weights are loaded into the actual modules, not the DDP wrapper
        accelerator.unwrap_model(G).load_state_dict(best_ckpt["G"])
        accelerator.unwrap_model(F).load_state_dict(best_ckpt["F"])
        accelerator.unwrap_model(DX).load_state_dict(best_ckpt["DX"])
        accelerator.unwrap_model(DY).load_state_dict(best_ckpt["DY"])

        # change to eval mode
        G.eval()
        F.eval()
        DX.eval()
        DY.eval()

        # evaluate on test set
        test_metrics = evaluate_epoch(
            G,
            F,  # generator models
            DX,
            DY,  # discriminator models
            test_loader,
            "test",
            mse,
            l1,
            lambda_adv,
            lambda_cyc,
            lambda_id,  # loss functions and loss params
            fid_metric,  # evaluation metric
            accelerator,
        )
        logger.info(f"Test metrics (best.pth):\n{test_metrics}")
        wandb.log(test_metrics)
        # write metrics out
        out_dir = Path(__file__).resolve().parents[2] / "outputs" / "metrics"
        out_dir.mkdir(parents=True, exist_ok=True)
        metrics_path = out_dir / "test_metrics.json"
        with metrics_path.open("w") as f:
            json.dump(test_metrics, f, indent=4)
        logger.info(f"Saved test metrics to {metrics_path}")
        # upload outputs to s3 bucket
        if cfg.archive_and_terminate_ec2:
            archive_ec2(bucket=cfg.s3_bucket_name, prefix=f"outputs/run-{run_name}")
    else:
        logger.info("Skipping test evaluation...")

    # Finished
    logger.info("Finished run.")

    # terminate ec2 instance
    if cfg.archive_and_terminate_ec2:
        terminate_ec2()


if __name__ == "__main__":
    main()
