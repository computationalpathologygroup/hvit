import os
import time
import tqdm
import wandb
import torch
import hydra
import statistics
import numpy as np
import pandas as pd
from pathlib import Path
from omegaconf import DictConfig

from source.models import ModelFactory
from source.components import LossFactory
from source.dataset import ExtractedFeaturesSurvivalDataset
from source.utils import (
    initialize_wandb,
    train_survival,
    tune_survival,
    test_survival,
    compute_time,
    log_on_step,
    EarlyStopping,
    OptimizerFactory,
    SchedulerFactory,
)


@hydra.main(
    version_base="1.2.0", config_path="../config/training/survival", config_name="multi"
)
def main(cfg: DictConfig):

    output_dir = Path(cfg.output_dir, cfg.experiment_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_root_dir = Path(output_dir, "checkpoints", cfg.level)
    checkpoint_root_dir.mkdir(parents=True, exist_ok=True)

    result_root_dir = Path(output_dir, "results", cfg.level)
    result_root_dir.mkdir(parents=True, exist_ok=True)

    # set up wandb
    if cfg.wandb.enable:
        key = os.environ.get("WANDB_API_KEY")
        _ = initialize_wandb(cfg, key=key)

    features_dir = Path(output_dir, "features", cfg.level)
    if cfg.features_dir:
        features_dir = Path(cfg.features_dir)

    fold_root_dir = Path(cfg.data.fold_dir)
    nfold = len([_ for _ in fold_root_dir.glob(f"fold_*")])
    print(f"Training on {nfold} folds")

    test_metrics = []

    start_time = time.time()
    for i in range(nfold):

        fold_dir = Path(fold_root_dir, f"fold_{i}")
        checkpoint_dir = Path(checkpoint_root_dir, f"fold_{i}")
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        result_dir = Path(result_root_dir, f"fold_{i}")
        result_dir.mkdir(parents=True, exist_ok=True)

        print(f"Loading data for fold {i}")
        train_df_path = Path(fold_dir, "train.csv")
        tune_df_path = Path(fold_dir, "tune.csv")
        test_df_path = Path(fold_dir, "tune.csv")
        train_df = pd.read_csv(train_df_path)
        tune_df = pd.read_csv(tune_df_path)
        test_df = pd.read_csv(test_df_path)

        if cfg.training.pct:
            print(f"Training on {cfg.training.pct*100}% of the data")
            train_df = train_df.sample(frac=cfg.training.pct).reset_index(drop=True)

        train_dataset = ExtractedFeaturesSurvivalDataset(
            train_df, features_dir, cfg.label_name, nbins=cfg.nbins
        )
        tune_dataset = ExtractedFeaturesSurvivalDataset(
            tune_df, features_dir, cfg.label_name, nbins=cfg.nbins
        )
        test_dataset = ExtractedFeaturesSurvivalDataset(
            test_df, features_dir, cfg.label_name, nbins=cfg.nbins
        )

        # train_c, tune_c, test_c = (
        #     train_dataset.num_classes,
        #     tune_dataset.num_classes,
        #     test_dataset.num_classes,
        # )
        # assert (
        #     train_c == tune_c == test_c
        # ), f"Different number of classes C in train (C={train_c}), tune (C={tune_c}) and test (C={test_c}) sets!"

        model = ModelFactory(cfg.level, cfg.nbins, cfg.model).get_model()
        model.relocate()
        print(model)

        model_params = filter(lambda p: p.requires_grad, model.parameters())
        optimizer = OptimizerFactory(
            cfg.optim.name, model_params, lr=cfg.optim.lr, weight_decay=cfg.optim.wd
        ).get_optimizer()
        scheduler = SchedulerFactory(optimizer, cfg.optim.lr_scheduler).get_scheduler()

        criterion = LossFactory(cfg.task, cfg.loss).get_loss()

        early_stopping = EarlyStopping(
            cfg.early_stopping.tracking,
            cfg.early_stopping.min_max,
            cfg.early_stopping.patience,
            cfg.early_stopping.min_epoch,
            checkpoint_dir=checkpoint_dir,
            save_all=cfg.early_stopping.save_all,
        )

        stop = False
        fold_start_time = time.time()

        if cfg.wandb.enable:
            wandb.define_metric(f"fold_{i}/train/epoch", summary="max")

        with tqdm.tqdm(
            range(cfg.nepochs),
            desc=(f"Fold {i} Training"),
            unit=" slide",
            ncols=100,
            leave=True,
        ) as t:

            for epoch in t:

                epoch_start_time = time.time()
                if cfg.wandb.enable:
                    wandb.log({f"fold_{i}/train/epoch": epoch})

                train_results = train_survival(
                    epoch + 1,
                    model,
                    train_dataset,
                    optimizer,
                    criterion,
                    batch_size=cfg.training.batch_size,
                    gradient_accumulation=cfg.training.gradient_accumulation,
                )

                if cfg.wandb.enable:
                    log_on_step(
                        f"fold_{i}/train",
                        train_results,
                        step=f"fold_{i}/train/epoch",
                        to_log=cfg.wandb.to_log,
                    )
                # train_dataset.df.to_csv(Path(result_dir, f"train_{epoch}.csv"), index=False)

                if epoch % cfg.tuning.tune_every == 0:

                    tune_results = tune_survival(
                        epoch + 1,
                        model,
                        tune_dataset,
                        criterion,
                        batch_size=cfg.tuning.batch_size,
                    )

                    if cfg.wandb.enable:
                        log_on_step(
                            f"fold_{i}/tune",
                            tune_results,
                            step=f"fold_{i}/train/epoch",
                            to_log=cfg.wandb.to_log,
                        )
                    # tune_dataset.df.to_csv(
                    #     Path(result_dir, f"tune_{epoch}.csv"), index=False
                    # )

                    early_stopping(epoch, model, tune_results)
                    if early_stopping.early_stop and cfg.early_stopping.enable:
                        stop = True

                if cfg.wandb.enable:
                    wandb.define_metric(
                        f"fold_{i}/train/lr", step_metric=f"fold_{i}/train/epoch"
                    )
                if scheduler:
                    lr = scheduler.get_last_lr()
                    if cfg.wandb.enable:
                        wandb.log({f"fold_{i}/train/lr": lr})
                    scheduler.step()
                elif cfg.wandb.enable:
                    wandb.log({f"fold_{i}/train/lr": cfg.optim.lr})

                epoch_end_time = time.time()
                epoch_mins, epoch_secs = compute_time(epoch_start_time, epoch_end_time)
                tqdm.tqdm.write(
                    f"End of epoch {epoch+1} / {cfg.nepochs} \t Time Taken:  {epoch_mins}m {epoch_secs}s"
                )

                if stop:
                    tqdm.tqdm.write(
                        f"Stopping early because best {cfg.early_stopping.tracking} was reached {cfg.early_stopping.patience} epochs ago"
                    )
                    break

        fold_end_time = time.time()
        fold_mins, fold_secs = compute_time(fold_start_time, fold_end_time)
        print(f"Total time taken for fold {i}: {fold_mins}m {fold_secs}s")

        # load best model
        best_model_fp = Path(checkpoint_dir, f"{cfg.testing.retrieve_checkpoint}_model.pt")
        if cfg.wandb.enable:
            wandb.save(str(best_model_fp))
        best_model_sd = torch.load(best_model_fp)
        model.load_state_dict(best_model_sd)

        test_results = test_survival(
            model,
            test_dataset,
            batch_size=1,
        )
        # test_dataset.df.to_csv(Path(result_dir, f"test.csv"), index=False)

        for r, v in test_results.items():
            if r == "c-index":
                test_metrics.append(v)
                v = round(v, 3)
            if r in cfg.wandb.to_log and cfg.wandb.enable:
                wandb.log({f"fold_{i}/test/{r}": v})

    mean_test_metric = round(np.mean(test_metrics), 3)
    std_test_metric = round(statistics.stdev(test_metrics), 3)
    if cfg.wandb.enable:
        wandb.log({f"test/c-index_mean": mean_test_metric})
        wandb.log({f"test/c-index_std": std_test_metric})

    end_time = time.time()
    mins, secs = compute_time(start_time, end_time)
    print(f"Total time taken ({nfold} folds): {mins}m {secs}s")


if __name__ == "__main__":

    main()
