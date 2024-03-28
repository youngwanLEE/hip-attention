import os
from pathlib import Path

import lightning as pl
import torch
import torch.autograd
import torch.onnx
import torch.utils.checkpoint
from deepspeed.ops.adam import DeepSpeedCPUAdam
from lightning.pytorch.callbacks import ModelCheckpoint
from lightning.pytorch.strategies import DeepSpeedStrategy
from pytorch_lightning.loggers.wandb import WandbLogger
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, random_split
from torch.utils.data import Subset

from timber.dataset.alpaca import AlpacaDataset
from timber.dataset.booksum import BookSumDataset
from timber.dataset.labdataset import LabDataset
from timber.dataset.openwebtext import OpenWebTextDataset
from timber.models.modeling_llama import LlamaDecoderLayer
from timber.trainer.common import TrainConfig, load_model, parse_args, load_tokenizer
from timber.utils import seed


# torch.autograd.set_detect_anomaly(True)
torch.set_float32_matmul_precision('high')


class LabDataModule(pl.LightningDataModule):
    def __init__(
            self,
            config: TrainConfig,
            num_workers: int = 0,
            data_dir: Path = "data",
            download: bool = True,
            train_size: float = 0.9,
    ):
        super().__init__()
        self.config = config
        self.data_dir = data_dir
        self.block_size = config.seq_len
        self.download = download
        self.num_workers = num_workers
        self.train_size = train_size
        self.dataset = None
        self.tokenizer = load_tokenizer(config.model)
        self.bsize = config.batch_size

    def prepare_data(self):
        if self.config.dataset in ['wikitext2', 'wikitext103']:
            self.dataset = LabDataset(
                data_dir=self.data_dir,
                block_size=self.block_size,
                download=self.download,
                tokenizer=self.tokenizer,
                dataset=self.config.dataset,
            )
        elif self.config.dataset in ['alpaca']:
            self.dataset = AlpacaDataset(
                tokenizer=self.tokenizer,
            )
        elif self.config.dataset in ['booksum']:
            self.dataset = BookSumDataset(
                tokenizer=self.tokenizer,
            )
        elif self.config.dataset in ['openwebtext']:
            self.dataset = OpenWebTextDataset(
                tokenizer=self.tokenizer,
                stride=self.block_size,
            )
        else:
            raise Exception()

    def setup(self, stage: str):
        if self.dataset is None:
            self.prepare_data()
        if self.config.dataset in ['wikitext2', 'wikitext103']:
            if stage == "fit" or stage is None:
                test_size = min(100, len(self.dataset) * (1 - self.train_size))
                train_size = int(len(self.dataset) - test_size)
                self.train_data, self.val_data = random_split(self.dataset, lengths=[train_size, test_size])
            if stage == "test" or stage is None:
                self.test_data = self.val_data
        elif self.config.dataset in ['booksum', 'alpaca', 'openwebtext']:
            if stage == "fit" or stage is None:
                def train_val_dataset(dataset, val_split=0.05):
                    train_idx, val_idx = train_test_split(list(range(len(dataset))), test_size=val_split)
                    train = Subset(dataset, train_idx)
                    valid = Subset(dataset, val_idx)
                    return train, valid

                self.train_data, self.val_data = train_val_dataset(self.dataset)
            if stage == "test" or stage is None:
                self.test_data = self.val_data
        else:
            raise Exception()

    def train_dataloader(self):
        return DataLoader(self.train_data, num_workers=self.num_workers, batch_size=self.bsize)

    def val_dataloader(self):
        return DataLoader(self.val_data, num_workers=self.num_workers, batch_size=self.bsize)

    def test_dataloader(self):
        return DataLoader(self.test_data, num_workers=self.num_workers, batch_size=self.bsize)


class LabModule(pl.LightningModule):
    def __init__(self, config: TrainConfig):
        super().__init__()

        self.model = load_model(train_config=config, method=config.method)
        if not config.disable_kd:
            self.teacher = load_model(train_config=config, method='none', is_teacher=True)
        else:
            self.teacher = None

        self.validation_preds = []
        self.validation_targets = []
        self.pad_token_id = self.model.base_model.config.pad_token_id
        self.config = config

    def forward(self, inputs, target, output_hidden_states=False):
        return self.model(
            inputs,
            attention_mask=(inputs != self.pad_token_id).to(inputs.dtype),
            labels=target,
            output_hidden_states=output_hidden_states
        )

    def training_step(self, batch, batch_idx):
        if self.teacher is not None:
            self.teacher.eval()
        self.model.train()

        inputs, target = batch

        inputs = inputs[:, :self.config.seq_len]
        target = target[:, :self.config.seq_len]

        # pad inputs and target
        inputs = torch.nn.functional.pad(inputs, (0, self.config.seq_len - inputs.shape[1]), value=self.pad_token_id)
        target = torch.nn.functional.pad(target, (0, self.config.seq_len - target.shape[1]), value=-100)

        if not self.config.disable_kd:
            with torch.no_grad():  # , torch.autocast('cuda', torch.bfloat16):
                output_teacher = self.teacher(inputs, output_hidden_states=not self.config.disable_kd)
        # with torch.autocast('cuda', torch.bfloat16):
        output = self(inputs, target, output_hidden_states=not self.config.disable_kd)
        logits = output.logits

        loss_model = torch.nn.functional.cross_entropy(
            logits.view(-1, logits.shape[-1]).to(torch.float32),
            target.view(-1)
        )

        loss_kd_hidden = 0
        loss_kd_logits = 0
        if not self.config.disable_kd:
            for teacher_layer, student_layer in zip(output_teacher.hidden_states, output.hidden_states):
                loss_kd_hidden += torch.nn.functional.mse_loss(student_layer.to(torch.float32),
                                                               teacher_layer.to(torch.float32))
            loss_kd_hidden = loss_kd_hidden / len(output_teacher.hidden_states)

            loss_kd_logits = torch.nn.functional.kl_div(
                output.logits.view(-1, logits.shape[-1]).to(torch.float32).log_softmax(-1),
                output_teacher.logits.view(-1, logits.shape[-1]).to(torch.float32).softmax(-1),
                reduction='batchmean',
            )

        if not self.config.disable_kd:
            loss = loss_model * 0.1 + (loss_kd_hidden + loss_kd_logits) * 2.5
        else:
            loss = loss_model

        self.log("training/loss_model", loss_model.item())
        if not self.config.disable_kd:
            if loss_kd_hidden > 0:
                self.log("training/loss_kd_hidden", loss_kd_hidden.item())
            if loss_kd_logits > 0:
                self.log("training/loss_kd_logits", loss_kd_logits.item())
        self.log("training/loss", loss.item())

        return loss

    def validation_step(self, batch, batch_idx):
        self.model.eval()

        inputs, target = batch
        inputs = inputs[:, :self.config.seq_len]
        target = target[:, :self.config.seq_len]
        with torch.no_grad():  # , torch.autocast('cuda', torch.bfloat16):
            # print('asdfasdf', inputs.shape, target.shape, flush=True)
            output = self(inputs, target).logits
            loss = torch.nn.functional.cross_entropy(
                output.view(-1, output.shape[-1]),
                target.view(-1)
            )
        self.log("val/loss", loss.item())

        self.validation_preds.append(output.cpu())
        self.validation_targets.append(target.cpu())

    def on_validation_epoch_end(self):
        from torchmetrics.text.perplexity import Perplexity
        with torch.no_grad():
            device = 'cpu'
            if self.config.using_fsdp:
                device = 'cuda'
            calculator = Perplexity(ignore_index=-100).to(device)
            for preds, target in zip(self.validation_preds, self.validation_targets):
                calculator.update(preds.to(device), target.to(device))
            ppl = calculator.compute()
        ppl = ppl.item()
        print('val/ppl', ppl)
        self.log("val/ppl", ppl)

        self.validation_preds.clear()
        self.validation_targets.clear()

    def configure_optimizers(self):
        params = []
        for name, p in self.model.named_parameters():
            # print(name, p.requires_grad, p.shape, p.dtype)
            if p.requires_grad:
                params.append(p)
        if self.config.using_fsdp:
            return DeepSpeedCPUAdam(params, lr=self.config.lr)
        # return DeepSpeedCPUAdam(params, lr=self.config.lr)
        return torch.optim.AdamW(params, lr=self.config.lr)


def main(config: TrainConfig):
    os.makedirs('./saves/dev/wandb', exist_ok=True)
    os.makedirs('./saves/dev/checkpoint', exist_ok=True)

    if config.using_fsdp:
        devices = torch.cuda.device_count()
        policy = {LlamaDecoderLayer}
        # strategy = FSDPStrategy(
        #     auto_wrap_policy=policy,
        #     activation_checkpointing_policy=policy,
        #     cpu_offload=True,
        # )
        # strategy = 'deepspeed_stage_3'
        deepspeed_config = {
            "zero_allow_untested_optimizer": True,
            "zero_optimization": {
                "stage": 3,
                "offload_param": {"device": "cpu"},
                "offload_optimizer": {"device": "cpu"},
                "max_live_parameters": 5e8,
                "max_reuse_distance": 1e8,
                "contiguous_gradients": True,
                "overlap_comm": False,
                "allgather_bucket_size": 1e7,
                "reduce_bucket_size": 1e7,
            },
        }
        strategy = DeepSpeedStrategy(config=deepspeed_config)
    else:
        devices = "1"
        strategy = "auto"

    if config.method == 'timber':
        filename = f'llama32k-{config.dataset}-{config.seq_len}-bq{config.block_size_q}-bk{config.block_size_k}-k{config.k}-{{epoch:02d}}-{{step}}'
    elif config.method == 'none':
        filename = f'llama32k-{config.dataset}-{config.seq_len}-{{epoch:02d}}-{{step}}'
    elif config.method == 'reformer':
        filename = f'llama32k-{config.method}-{config.dataset}-{config.seq_len}-k{config.k}-{{epoch:02d}}-{{step}}'
    elif config.method == 'performer':
        filename = f'llama32k-{config.method}-{config.dataset}-{config.seq_len}-{{epoch:02d}}-{{step}}'
    else:
        raise Exception()

    checkpoint_callback = ModelCheckpoint(
        save_top_k=3,
        monitor="step",
        mode="max",
        dirpath=config.model_checkpoint_dir,
        filename=filename,
        every_n_train_steps=config.save_steps,
        enable_version_counter=False,
    )
    checkpoint_callback.CHECKPOINT_EQUALS_CHAR = '-'
    checkpoint_callback.FILE_EXTENSION = '.pth'

    trainer = pl.Trainer(
        log_every_n_steps=1,
        devices=devices,
        accelerator="gpu",
        strategy=strategy,
        precision=16,
        default_root_dir=config.model_checkpoint_dir,
        accumulate_grad_batches=config.accumulation_steps,
        max_epochs=20,
        max_steps=config.max_steps,
        logger=WandbLogger(
            save_dir="saves/dev/wandb",
            project="timber-attention"
        ),
        enable_checkpointing=True,
        callbacks=[
            checkpoint_callback
        ],
    )

    datamodule = LabDataModule(config=config)
    model = LabModule(config=config)
    kwargs = dict(
        model=model,
        datamodule=datamodule,
    )
    if config.load_from_checkpoint is not None:
        kwargs['ckpt_path'] = config.load_from_checkpoint
    trainer.fit(**kwargs)


if __name__ == "__main__":
    seed()
    main(parse_args())
