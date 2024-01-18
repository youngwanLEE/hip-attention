import gc
import os
from dataclasses import asdict, dataclass
from os import PathLike
from pathlib import Path

# import mlflow
import torch
import torch.onnx
import lightning as pl
from lightning import Trainer
from lightning.pytorch.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.callbacks import EarlyStopping
from pytorch_lightning.loggers.wandb import WandbLogger
from pytorch_lightning.profilers import PyTorchProfiler

import transformers

from src.models.modeling_llama import LlamaForCausalLM, LlamaConfig

import os
from dataclasses import dataclass, field
from pathlib import Path

from src.dataset.labdataset import LabDataset
from torch.utils.data import DataLoader, random_split

@dataclass
class TrainConfig:
    lr: float = 1e-4
    batch_size: int = 256
    model_checkpoint_dir: str = "./saves/dev"

class LabDataModule(pl.LightningDataModule):
    def __init__(
        self,
        num_workers: int = 0,
        data_dir: Path ="data",
        batch_size: int = 1,
        block_size: int = 4096,
        download: bool = True,
        train_size: float = 0.9,
    ):
        super().__init__()
        self.data_dir = data_dir
        self.block_size = block_size
        self.download = download
        self.num_workers = num_workers
        self.train_size = train_size
        self.dataset = None
        self.tokenizer = load_tokenizer()
        self.bsize = batch_size
    
    def prepare_data(self):
        self.dataset = LabDataset(
            data_dir=self.data_dir,
            block_size=self.block_size,
            download=self.download,
            tokenizer=self.tokenizer
        )
    
    def setup(self, stage: str):
        if stage == "fit" or stage is None:
            train_size = int(len(self.dataset) * self.train_size)
            test_size = len(self.dataset) - train_size
            self.train_data, self.val_data = random_split(self.dataset, lengths=[train_size, test_size])
        if stage == "test" or stage is None:
            self.test_data = self.val_data

    def train_dataloader(self):
        return DataLoader(self.train_data, num_workers=self.num_workers, batch_size=self.bsize)

    def val_dataloader(self):
        return DataLoader(self.val_data, num_workers=self.num_workers, batch_size=self.bsize)

    def test_dataloader(self):
        return DataLoader(self.test_data, num_workers=self.num_workers, batch_size=self.bsize)

from peft import LoraConfig, TaskType
from peft import get_peft_model, prepare_model_for_kbit_training

def load_model(method = 'tree', device = 'cuda:0'):
    model_id = 'togethercomputer/LLaMA-2-7B-32K'
    config = LlamaConfig.from_pretrained(model_id)
    config._attn_implementation = config.attn_implementation = 'sdpa'
    
    model = LlamaForCausalLM.from_pretrained(
        model_id,
        config=config, 
        load_in_4bit=True,
        device_map={"" : device},
        quantization_config=transformers.BitsAndBytesConfig(
            load_in_4bit=True,
            llm_int8_skip_modules=['tree_avgpool_scaler'],
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        ),
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
    )
    
    for m in model.modules():
        if hasattr(m, 'attention_method'):
            m.attention_method = method
    
    if method != 'none':
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=64,
            lora_alpha=32, 
            lora_dropout=0.1,
            modules_to_save=['tree_avgpool_scaler']
        )
        
        model = prepare_model_for_kbit_training(model)
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
    
    return model

def load_tokenizer():
    model_id = 'togethercomputer/LLaMA-2-7B-32K'
    tokenizer = transformers.AutoTokenizer.from_pretrained(model_id)
    return tokenizer

class LabModule(pl.LightningModule):
    def __init__(self):
        super().__init__()
        
        self.model = load_model()
        self.teacher = load_model(method = 'none')
        
        self.validation_preds = []
        self.validation_targets = []

    def forward(self, inputs, target, output_hidden_states=False):
        return self.model(
            inputs, 
            target, 
            output_hidden_states=output_hidden_states
        )

    def training_step(self, batch, batch_idx):
        inputs, target = batch
        
        with torch.no_grad():
            output_teacher = self.teacher(inputs, output_hidden_states=True)
        output = self(inputs, target, output_hidden_states=True)
        logits = output.logits
        
        loss_model = torch.nn.functional.cross_entropy(
            logits.view(-1, logits.shape[-1]).to(torch.float32),
            target.view(-1)
        )
        
        loss_kd_hidden = 0
        for teacher_layer, student_layer in zip(output_teacher.hidden_states, output.hidden_states):
            loss_kd_hidden += torch.nn.functional.mse_loss(student_layer.to(torch.float32), teacher_layer.to(torch.float32))
        loss_kd_hidden = loss_kd_hidden / len(output_teacher.hidden_states)
        
        loss_kd_logits = torch.nn.functional.kl_div(
            output.logits.view(-1, logits.shape[-1]).to(torch.float32).log_softmax(-1),
            output_teacher.logits.view(-1, logits.shape[-1]).to(torch.float32).softmax(-1),
            reduction='batchmean',
        )
        
        loss = loss_model * 0.1 + loss_kd_hidden + loss_kd_logits
        
        self.log("training/loss_model", loss_model.item())
        self.log("training/loss_kd_hidden", loss_kd_hidden.item())
        self.log("training/loss_kd_logits", loss_kd_logits.item())
        self.log("training/loss", loss.item())
        
        return loss
    
    def validation_step(self, batch, batch_idx):
        inputs, target = batch
        with torch.no_grad():
            output = self(inputs, target).logits
        loss = torch.nn.functional.cross_entropy(
            output.view(-1, output.shape[-1]), 
            target.view(-1)
        )
        self.log("val-loss", loss.item())
        
        self.validation_preds.append(output.cpu())
        self.validation_targets.append(target.cpu())
    
    def on_validation_epoch_end(self):
        from torchmetrics.text.perplexity import Perplexity
        calculator = Perplexity(ignore_index=-100)
        for preds, target in zip(self.validation_preds, self.validation_targets):
            calculator.update(preds, target)
        ppl = calculator.compute()
        ppl = ppl.item()
        print('val-ppl', ppl)
        self.log("val-ppl", ppl)
        
        self.validation_preds.clear()
        self.validation_targets.clear()
        
    def configure_optimizers(self):
        params = []
        for name, p in self.model.named_parameters():
            print(name, p.requires_grad, p.shape, p.dtype)
            if p.requires_grad:
                params.append(p)
        return torch.optim.AdamW(params, lr=0.0001)

def main(config: TrainConfig):
    os.makedirs('./saves/dev/wandb', exist_ok=True)
    os.makedirs('./saves/dev/checkpoint', exist_ok=True)
    
    checkpoint_callback = ModelCheckpoint(
        save_top_k=3,
        monitor="step",
        mode="max",
        dirpath="saves/dev/checkpoint",
        filename="llama32k-wikitext2-{epoch:02d}-{step}",
        every_n_train_steps=50,
    )
    
    trainer = pl.Trainer(
        log_every_n_steps=1,
        devices="1",
        accelerator="gpu",
        strategy="auto",
        precision="32-true",
        default_root_dir='./saves/dev/checkpoint/',
        enable_checkpointing=True,
        accumulate_grad_batches=4,
        max_epochs=4,
        logger=WandbLogger(save_dir="saves/dev/wandb"),
        callbacks=[
            checkpoint_callback
        ],
    )
    
    datamodule = LabDataModule()
    model = LabModule()
    trainer.fit(model=model, datamodule=datamodule) 

if __name__ == "__main__":
    train_config = TrainConfig()
    main(train_config)