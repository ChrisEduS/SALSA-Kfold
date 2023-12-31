"""
This module includes code to train SED task. Input is a segment/chunk of 60-second audio clips.
"""
import fire
import logging
import os

import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger

from utilities.builder_utils import build_database, build_datamodule, build_model, build_task
from utilities.experiments_utils import manage_experiments
from utilities.learning_utils import LearningRateScheduler, MyLoggingCallback

global val_perm

def train(exp_config: str = './configs/seld.yml',
          exp_group_dir: str = '/media/tho_nguyen/disk2/new_seld/dcase2021/outputs',
          exp_suffix: str = '_test',
          resume: bool = False):
    """
    Training script
    :param exp_config: Config file for experiments
    :param exp_group_dir: Parent directory to store all experiment results.
    :param exp_suffix: Experiment suffix.
    :param resume: If true, resume training from the last epoch.
    """
    # Load config, create folders, logging
    curr_perm = 1
    while curr_perm < 6:
        cfg = manage_experiments(exp_config=exp_config, exp_group_dir=exp_group_dir, exp_suffix=exp_suffix, is_train=True)
        logger = logging.getLogger('lightning')

        # Set random seed for reproducible
        pl.seed_everything(cfg.seed)

        # Resume training
        if resume:
            ckpt_list = [f for f in os.listdir(cfg.dir.model.checkpoint) if f.startswith('epoch') and f.endswith('ckpt')]
            if len(ckpt_list) > 0:
                resume_from_checkpoint = os.path.join(cfg.dir.model.checkpoint, sorted(ckpt_list)[-1])
                logger.info('Found checkpoint to be resume training at {}'.format(resume_from_checkpoint))
                curr_perm = int(resume_from_checkpoint[-6]) # Consider checkpoint format is something like this epoch=002perm4.ckpt
                resume = False
            else:
                resume_from_checkpoint = None
        else:
            resume_from_checkpoint = None

        # Load feature database
        feature_db = build_database(cfg=cfg)


        #FROM HERE THE SCRIPT IS LOADING THE DATA AND TRAINING IT. 
        # Load data module
        datamodule = build_datamodule(cfg=cfg, feature_db=feature_db)
        datamodule.set_val_perm(curr_perm)
        datamodule.setup(stage='fit')
        steps_per_train_epoch = int(len(datamodule.train_dataloader()) * cfg.data.train_fraction)

        # Set learning params
        lr_scheduler = LearningRateScheduler(steps_per_epoch=steps_per_train_epoch, max_epochs=cfg.training.max_epochs,
                                            milestones=cfg.training.lr_scheduler.milestones,
                                            lrs=cfg.training.lr_scheduler.lrs, moms=cfg.training.lr_scheduler.moms)
        logger.info('Finish configuring learning rate scheduler.')

        # # Model checkpoint
        model_checkpoint = ModelCheckpoint(dirpath=cfg.dir.model.checkpoint, filename='{epoch:03d}'+'perm'+str(curr_perm))  # also save last model
        save_best_model = ModelCheckpoint(monitor='valSeld', mode='min', period=cfg.training.val_interval,
                                        dirpath=cfg.dir.model.best, save_top_k=1,
                                        filename='{epoch:03d}-{valSeld:.3f}-{valER:.3f}-{valF1:.3f}-{valLE:.3f}-'
                                                '{valLR:.3f}-'+'perm'+str(curr_perm))

        # Console logger
        console_logger = MyLoggingCallback(curr_perm)

        # Tensorboard logger
        tb_logger = TensorBoardLogger(save_dir=cfg.dir.tb_dir, name='my_model')

        # Build encoder and decoder
        encoder_params = cfg.model.encoder.__dict__
        encoder = build_model(**encoder_params)
        decoder_params = cfg.model.decoder.__dict__
        decoder_params = {'n_output_channels': encoder.n_output_channels, 'n_classes': cfg.data.n_classes,
                        'output_format': cfg.data.output_format, **decoder_params}
        decoder = build_model(**decoder_params)

        # Build Lightning model
        submission_dir = os.path.join(cfg.dir.output_dir.submission, '_temp')  # to temporarily store val output
        os.makedirs(submission_dir, exist_ok=True)
        model = build_task(encoder=encoder, decoder=decoder, cfg=cfg, submission_dir=submission_dir,
                        test_chunk_len=feature_db.test_chunk_len, test_chunk_hop_len=feature_db.test_chunk_hop_len)

        # Train
        callback_list = [lr_scheduler, console_logger, model_checkpoint]
        if cfg.mode == 'crossval':
            callback_list.append(save_best_model)
            max_epochs = cfg.training.max_epochs
        elif cfg.mode == 'eval':
            max_epochs = cfg.training.best_epoch
        else:
            raise ValueError('Invalid mode {}'.format(cfg.mode))
        #
        trainer = pl.Trainer(gpus=torch.cuda.device_count(), resume_from_checkpoint=resume_from_checkpoint,
                            max_epochs=max_epochs, logger=tb_logger, progress_bar_refresh_rate=2,
                            check_val_every_n_epoch=cfg.training.val_interval,
                            log_every_n_steps=100, flush_logs_every_n_steps=200,
                            limit_train_batches=cfg.data.train_fraction, limit_val_batches=cfg.data.val_fraction,
                            callbacks=callback_list)
        trainer.fit(model, datamodule)
        if cfg.mode == 'crossval':
            logger.info('Best model checkpoint: {}'.format(save_best_model.best_model_path))

        curr_perm += 1
        # Test: lightning default takes the best model
        # trainer.test()
        # HERE THE SCRIPT IS SAVING THE MODEL. RETRAIN WITH THE BEST IN EVERY FOLD.

if __name__ == '__main__':        
    fire.Fire(train)
