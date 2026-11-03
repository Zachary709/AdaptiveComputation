"""Small, deterministic CNN smoke workload for OpenSMI.

It intentionally generates data locally so the parallel-dispatch test does not
need a dataset download or shared filesystem.  Each process trains an
independent model and writes a short JSONL record to the selected data disk.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

import torch
from torch import nn


class TinyCNN(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 8, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv2d(8, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, 1)),
        )
        self.classifier = nn.Linear(16, 10)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(inputs).flatten(1))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--world-size", type=int, required=True)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.steps < 1 or args.batch_size < 1:
        raise SystemExit("steps and batch-size must be positive")

    seed = 20260916 + args.rank
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        device = torch.device("cuda")
        device_name = torch.cuda.get_device_name(0)
    else:
        device = torch.device("cpu")
        device_name = "cpu"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record = {
        "event": "start",
        "node": socket.gethostname(),
        "rank": args.rank,
        "world_size": args.world_size,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "device": device_name,
    }
    with args.output.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        print(json.dumps(record, sort_keys=True), flush=True)

        model = TinyCNN().to(device)
        optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
        criterion = nn.CrossEntropyLoss()
        final_loss = 0.0
        for step in range(args.steps):
            inputs = torch.randn(args.batch_size, 1, 32, 32, device=device)
            labels = torch.randint(0, 10, (args.batch_size,), device=device)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(inputs), labels)
            loss.backward()
            optimizer.step()
            final_loss = float(loss.detach().cpu())
            if device.type == "cuda":
                torch.cuda.synchronize()
            if step == 0 or step + 1 == args.steps:
                progress = {
                    "event": "progress",
                    "node": socket.gethostname(),
                    "rank": args.rank,
                    "step": step + 1,
                    "steps": args.steps,
                    "loss": final_loss,
                }
                stream.write(json.dumps(progress, sort_keys=True) + "\n")
                stream.flush()
                print(json.dumps(progress, sort_keys=True), flush=True)

        finished = {
            "event": "done",
            "node": socket.gethostname(),
            "rank": args.rank,
            "world_size": args.world_size,
            "device": device_name,
            "steps": args.steps,
            "loss": final_loss,
            "elapsed_s": round(time.time() - started, 3),
        }
        stream.write(json.dumps(finished, sort_keys=True) + "\n")
        stream.flush()
        print(json.dumps(finished, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
