import os
import time
import wandb
import torch.nn as nn
import torch.optim as optim
import hydra
import pandas as pd
from pathlib import Path
from omegaconf import OmegaConf

from source.models import HIPT
from source.dataset import ExtractedFeaturesDataset
from source.utils import initialize_wandb, create_train_tune_test_df, train, tune, epoch_time, EarlyStopping


@hydra.main(version_base='1.2.0', config_path='config', config_name='local')
def main(cfg):

    output_dir = Path(cfg.output_dir, cfg.dataset_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_dir = Path(output_dir, 'checkpoints', cfg.level)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    # set up wandb
    key = os.environ.get('WANDB_API_KEY')
    config = OmegaConf.to_container(cfg, resolve=True, throw_on_missing=True)
    _ = initialize_wandb(project=cfg.wandb.project, exp_name=cfg.wandb.exp_name, entity=cfg.wandb.username, config=config, key=key)
    wandb.define_metric('epoch', summary='max')

    if cfg.features_dir:
        features_dir = Path(cfg.features_dir)
    else:
        features_dir = Path(output_dir, 'features', cfg.level)

    model = HIPT(
        level=cfg.level,
        num_classes=cfg.num_classes,
        pretrain_4096=cfg.pretrain_4096,
        freeze_4096=cfg.freeze_4096,
        dropout=cfg.dropout,
    )
    model.relocate()

    fold_num = cfg.fold_num
    fold_dir = Path(cfg.data_dir, cfg.dataset_name, 'splits', f'fold_{fold_num}')

    if Path(fold_dir, 'train.csv').exists() and Path(fold_dir, 'tune.csv').exists() and Path(fold_dir, 'test.csv').exists():
        print(f'Loading data for fold {fold_num}')
        train_df_path = Path(fold_dir, 'train.csv')
        tune_df_path = Path(fold_dir, 'tune.csv')
        test_df_path = Path(fold_dir, 'test.csv')
        train_df = pd.read_csv(train_df_path)
        tune_df = pd.read_csv(tune_df_path)
        test_df = pd.read_csv(test_df_path)
    else:
        label_df = pd.read_csv(cfg.data_csv)
        if cfg.slide_list:
            with open(Path(cfg.slide_list), 'r') as f:
                slides = sorted([Path(x.strip()).stem for x in f.readlines()])
            print(f'Restricting data to the {len(slides)} slides in slide list .txt file provided')
            label_df = label_df[label_df['slide_id'].isin(slides)]
        train_df, tune_df, test_df = create_train_tune_test_df(label_df, save_csv=False, output_dir=cfg.data_dir)

    if cfg.pct:
        print(f'Training & Tuning on {cfg.pct*100}% of the data')
        train_df = train_df.sample(frac=cfg.pct).reset_index(drop=True)
        tune_df = tune_df.sample(frac=cfg.pct).reset_index(drop=True)

    train_dataset = ExtractedFeaturesDataset(train_df, features_dir)
    tune_dataset = ExtractedFeaturesDataset(tune_df, features_dir)
    assert train_dataset.num_classes == tune_dataset.num_classes

    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=cfg.lr, weight_decay=cfg.wd)
    if cfg.lr_scheduler:
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=cfg.lr_step, gamma=0.1)

    criterion = nn.CrossEntropyLoss()

    early_stopping = EarlyStopping(
        cfg.early_stopping.tracking,
        cfg.early_stopping.min_max,
        cfg.early_stopping.patience,
        cfg.early_stopping.min_epoch,
        checkpoint_dir=checkpoint_dir,
        save_all=cfg.save_all,
    )

    stop = False
    for epoch in range(cfg.nepochs):

        start_time = time.time()
        wandb.log({'epoch': epoch})

        train_results = train(
            epoch+1,
            model,
            train_dataset,
            optimizer,
            criterion,
            batch_size=cfg.train_batch_size,
            weighted_sampling=cfg.weighted_sampling,
            gradient_clipping=cfg.gradient_clipping,
        )

        for res, val in train_results.items():
            wandb.define_metric(f'train/{res}', step_metric='epoch')
            wandb.log({f'train/{res}': val})

        if epoch % cfg.tune_every == 0:

            tune_results = tune(
                epoch+1,
                model,
                tune_dataset,
                criterion,
                batch_size=cfg.tune_batch_size
            )

            for res, val in tune_results.items():
                wandb.define_metric(f'tune/{res}', step_metric='epoch')
                wandb.log({f'tune/{res}': val})

            early_stopping(epoch, model, tune_results)
            if early_stopping.early_stop:
                stop = True

        if cfg.lr_scheduler:
            scheduler.step()

        end_time = time.time()
        epoch_mins, epoch_secs = epoch_time(start_time, end_time)
        print(f'End of epoch {epoch+1} / {cfg.nepochs} \t Time Taken:  {epoch_mins}m {epoch_secs}s')

        if stop:
            print('Early stopping')
            break


if __name__ == '__main__':

    # python3 train_global.py
    main()


