import os
import time
import traceback
import torch
import transformers
from datasets import load_dataset
from tqdm import tqdm
import argparse, json
from transformers import TextStreamer

from peft import LoraConfig, TaskType
from peft import get_peft_model, prepare_model_for_kbit_training
from timber.models.modeling_llama import LlamaForCausalLM, LlamaConfig
from timber.utils import seed, get_bench

def job_ppl(args, model, tokenizer, device):
    outfile = f'./cache/llama_eval/ppl_{args.method}_{args.model}_s{args.stride}_dl{args.dense_layers}_k{args.k}_ckpt{args.checkpoint is not None}.json'
    print("Will write to", outfile)
    if os.path.exists(outfile):
        print(f'PPL already computed, skipping: {outfile}')
        return

    os.makedirs('./cache', exist_ok=True)
    cache_path = './cache/llama_eval.pth'
    if not os.path.exists(cache_path):
        test = load_dataset("wikitext", "wikitext-2-raw-v1", split="test")
        encodings = tokenizer("\n\n".join(test["text"]), return_tensors="pt").input_ids
        torch.save(encodings, cache_path)
    else:
        encodings = torch.load(cache_path)

    max_length = model.config.max_position_embeddings
    max_length = stride = args.stride if args.stride > 0 else model.config.max_position_embeddings
    seq_len = encodings.size(1)

    nlls = []
    prev_end_loc = 0
    with tqdm(range(0, seq_len, stride)[:args.count]) as pbar:
        for begin_loc in pbar:
            end_loc = min(begin_loc + max_length, seq_len)
            trg_len = end_loc - prev_end_loc  # may be different from stride on last loop
            input_ids = encodings[:, begin_loc:end_loc].to(device)
            target_ids = input_ids.clone()
            target_ids[:, :-trg_len] = -100

            with torch.no_grad():
                outputs = model(
                    input_ids,
                    labels=target_ids,
                )
                neg_log_likelihood = outputs.loss

            nlls.append(neg_log_likelihood.cpu())

            prev_end_loc = end_loc
            
            ppl = torch.exp(torch.stack(nlls).mean()).item()
            pbar.set_description(f"ppl: {ppl:.3f}")
            
            if end_loc == seq_len:
                break

    ppl = torch.exp(torch.stack(nlls).mean()).item()
    
    os.makedirs('./cache/llama_eval/', exist_ok=True)
    with open(outfile, 'w') as f:
        json.dump({'ppl': ppl}, f)

    print(f'PPL: {ppl:.4f}')
