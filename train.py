# train.py
import argparse
import json
import logging
import os

import torch
from transformers import GPT2LMHeadModel, GPT2Tokenizer
import wandb

from advanced_ppo_trainer import AdvancedPPOTrainer
from baseline_network import BaselineNetwork
from data_handler import DataHandler
from enhanced_env import EnhancedQuantizationEnv, LayerStats
from policy import PolicyNetPPO
from quantizer import Quantizer

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", type=str, required=True, help="Name for the experiment")
    parser.add_argument("--model", type=str, default="gpt2", help="Model name (default: gpt2)")
    parser.add_argument("--dataset", type=str, default="commonsense_qa", help="Dataset name (default: commonsense_qa)")
    parser.add_argument("--rl_algorithm", type=str, default="ppo", choices=["ppo"], help="RL algorithm (default: ppo)")
    parser.add_argument("--batch_size", type=int, default=16, help="Batch size for training (default: 16)")
    parser.add_argument("--episodes", type=int, default=10, help="Number of RL episodes (default: 10)")
    parser.add_argument("--state_dim", type=int, default=10, help="State dimension for policy network (default: 10)")
    parser.add_argument("--hidden_dim", type=int, default=64, help="Hidden dimension for policy network (default: 64)")
    parser.add_argument("--gamma", type=float, default=0.99, help="Discount factor (default: 0.99)")
    parser.add_argument("--clip_ratio", type=float, default=0.2, help="PPO clipping parameter (default: 0.2)")
    parser.add_argument("--lr", type=float, default=1e-3, help="Learning rate for policy network (default: 1e-3)")
    parser.add_argument("--finetune_steps", type=int, default=5, help="Number of fine-tuning steps per layer (default: 5)")
    parser.add_argument("--quant_types", type=str, default="nf4,fp4,int8,fp16", help="Supported quantization types: nf4, fp4, int8, fp16, bf16, fp32")
    parser.add_argument("--reference_model_dir", type=str, default=None, help="Directory for the fine-tuned reference model")

    args = parser.parse_args()

    quant_types = args.quant_types.split(',')

    output_dir = os.path.join("results", args.name)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Using device: {device}")

    tokenizer = GPT2Tokenizer.from_pretrained(
        args.model,
        #force_download=True,
        local_files_only=False)

    tokenizer.pad_token = tokenizer.eos_token

    wandb.init(project="DynaQ")  # Initialize wandb

    model = GPT2LMHeadModel.from_pretrained(
        args.model,
        #force_download=True,
        local_files_only=False).to(device)
    model.to(torch.float32)

    if args.reference_model_dir is not None:
        reference_model = GPT2LMHeadModel.from_pretrained(args.reference_model_dir).to(device)
    else:
        reference_model = GPT2LMHeadModel.from_pretrained(args.model).to(device) #we start with the same model

    reference_model.to(torch.float32)

    data_handler = DataHandler(dataset_name=args.dataset, batch_size=args.batch_size, max_length=128)
    train_loader, val_loader = data_handler.load_dataset()

    quantizer = Quantizer(compute_dtype=torch.float16, target_device=device)

    reward_weights = {
        'performance': 6.0, #1.0,
        'kl': 0.1,
        'entropy': 0.05,
        'memory': 2.5#0.5 #2.5,  # 0.2, #0.3, #0.85, #1.0
    }

    env = EnhancedQuantizationEnv(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        quantizer=quantizer,
        reference_model=reference_model,
        finetune_steps=args.finetune_steps,
        reward_weights=reward_weights,
        lr=5e-5,
        device=device
    )

    policy = PolicyNetPPO(args.state_dim, args.hidden_dim, num_actions=len(quant_types)).to(device)
    baseline = BaselineNetwork(args.state_dim, args.hidden_dim, lr=1e-3).to(device)

    # hyperparameters for saving
    hyperparams = {
        "state_dim": args.state_dim,
        "hidden_dim": args.hidden_dim,
        "quant_types": quant_types,
        "gamma": args.gamma,
        "clip_ratio": args.clip_ratio,
        "lr": args.lr,
        "finetune_steps": args.finetune_steps,
        "episodes": args.episodes
    }

    trainer = AdvancedPPOTrainer(
        env=env,
        policy=policy,
        baseline=baseline,
        quant_types=quant_types,
        gamma=args.gamma,
        clip_ratio=args.clip_ratio,
        lr=args.lr,
        train_policy_iters=3,
        results_path=output_dir
    )

    all_rewards, ep_infos = trainer.train(num_episodes=args.episodes)

    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"Saving results to {output_dir}")
    torch.save(policy.state_dict(), os.path.join(output_dir, "policy.pt"))
    torch.save(baseline.state_dict(), os.path.join(output_dir, "baseline.pt"))
    save_results(output_dir, hyperparams, reward_weights, all_rewards, ep_infos)
    logger.info(f"Results saved to {output_dir}/results.json")

    logger.info("Done!")


def save_results(output_dir,
                 hyperparams=None,
                 reward_weights=None,
                 all_rewards=None,
                 info_stats=None):

    with open(os.path.join(output_dir, "hyperparams.json"), "w") as f:
        json.dump(hyperparams, f, indent=2)

    with open(os.path.join(output_dir, "reward_weights.json"), "w") as f:
        json.dump(reward_weights, f, indent=2)

    with open(os.path.join(output_dir, "all_rewards.json"), "w") as f:
        json.dump(all_rewards, f, indent=2)

    from dataclasses import asdict
    cleaned_info_stats = []
    for entry in info_stats:
        cleaned_entry = {}
        for k, v in entry.items():
            if isinstance(v, LayerStats):
                cleaned_entry[k] = asdict(v)
            else:
                cleaned_entry[k] = v
        cleaned_info_stats.append(cleaned_entry)

    # dump cleaned_info_stats
    with open(os.path.join(output_dir, "info_stats.json"), "w") as f:
        json.dump(cleaned_info_stats, f, indent=2)

    wandb.finish()

if __name__ == "__main__":
    main()

# example usage:
# python train.py --name gpt-2-EAQ-100-v1 --episodes 100 --finetune_steps 5 --quant_types nf4,fp4,int8,fp16