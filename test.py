import torch
import argparse
import pathlib
import torchaudio
import numpy as np
from tqdm import tqdm
import yaml
from importlib import import_module
import pyworld as pw
from functools import partial

from datasets.mpop600 import MPop600Dataset
from loss.spec import MSSLoss
from ltng.vocoder import DDSPVocoder

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def get_instance(config):
    module_path, class_name = config["class_path"].rsplit(".", 1)
    module = import_module(module_path)
    return getattr(module, class_name)(**config.get("init_args", {}))

def freq2cent(f0):
    return 1200 * np.log2(f0 / 440)




@torch.no_grad()
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=str, help='Path to config file')
    parser.add_argument('ckpt', type=str, help='Path to checkpoint file')
    parser.add_argument('data', type=str, help='Path to data directory')
    parser.add_argument('--valid', action='store_true', help='Run on validation')
    args = parser.parse_args()


    with open(args.config) as f:
        config = yaml.safe_load(f)

    model_configs = config["model"]

    model_configs["feature_trsfm"]["init_args"]["sample_rate"] = model_configs["sample_rate"]
    model_configs["feature_trsfm"]["init_args"]["window"] = model_configs["window"]
    model_configs["feature_trsfm"]["init_args"]["hop_length"] = model_configs["hop_length"]
    model_configs["feature_trsfm"] = get_instance(model_configs["feature_trsfm"])

    model_configs["encoder"] = get_instance(model_configs["encoder"])
    model_configs["criterion"] = get_instance(model_configs["criterion"])

    model_configs["decoder"]["init_args"]["harm_oscillator"] = get_instance(model_configs["decoder"]["init_args"]["harm_oscillator"])
    model_configs["decoder"]["init_args"]["noise_generator"] = get_instance(model_configs["decoder"]["init_args"]["noise_generator"])
    
    if model_configs["decoder"]["init_args"]["harm_filter"] is not None:
        model_configs["decoder"]["init_args"]["harm_filter"] = get_instance(model_configs["decoder"]["init_args"]["harm_filter"])
    if model_configs["decoder"]["init_args"]["noise_filter"] is not None:
        model_configs["decoder"]["init_args"]["noise_filter"] = get_instance(model_configs["decoder"]["init_args"]["noise_filter"])
    if model_configs["decoder"]["init_args"]["end_filter"] is not None:
        model_configs["decoder"]["init_args"]["end_filter"] = get_instance(model_configs["decoder"]["init_args"]["end_filter"])
    
    
    model_configs["decoder"] = get_instance(model_configs["decoder"])

    model = DDSPVocoder.load_from_checkpoint(args.ckpt, **model_configs).to(device)
    model.eval()

    if args.valid:
        data_postfix = MPop600Dataset.valid_file_postfix
    else:
        data_postfix = MPop600Dataset.test_file_postfix

    metric1 = MSSLoss([512, 1024, 2048], window="hanning").to(device)
    # metric2 = torch.nn.L1Loss().to(device)
    get_f0 = partial(pw.dio, 
            f0_floor=65,
            f0_ceil=1047,
            channels_in_octave=2,
            frame_period=5,
        )

    wav_dir = pathlib.Path(args.data)

    total_valid_f0_frames = 0
    losses = []
    frames = []
    total_f0_loss = 0

    pbar = tqdm([f for f in wav_dir.glob("*.wav") if f.name.split("_")[1] in data_postfix])

    for f in pbar:
        x, sr = torchaudio.load(f)
        x = x.to(device)
        f0 = np.loadtxt(f.with_suffix(".pv"))
        # f0 = torch.from_numpy(f0).to(device)
        
        # mss loss
        mel = model.feature_trsfm(x)
        f0_hat, *_, x_hat = model(mel)
        x_hat = x_hat[:, : x.shape[1]]
        x = x[:, : x_hat.shape[1]]
        loss = metric1(x_hat, x)

        # f0 loss in cent
        f0_hat = get_f0(x_hat.squeeze().cpu().numpy(), sr)
        f0 = f0[: f0_hat.shape[0]]
        f0_hat = f0_hat[: f0.shape[0]]
        f0_mask = f0 >= 80
        f0_masked = f0[f0_mask]
        f0_hat_masked = f0_hat[f0_mask]
        f0_loss = np.mean(np.abs(freq2cent(f0_hat_masked) - freq2cent(f0_masked)))
        # f0_loss = metric2(freq2cent(f0_hat_masked), freq2cent(f0_masked))

        pbar.set_description(f"Loss: {loss.item():.4f}, F0 Loss: {f0_loss.item():.4f}")

        losses.append(loss.item())
        frames.append(x.shape[1])

        num_validframes = np.count_nonzero(f0_mask)
        total_valid_f0_frames += num_validframes
        total_f0_loss += f0_loss.item() * num_validframes
    
    total_frames = sum(frames)
    avg_loss = np.average(losses, weights=[f / total_frames for f in frames])
    print(total_frames, total_valid_f0_frames)
    print(f"Loss: {avg_loss:.4f}, F0 Loss: {total_f0_loss / total_valid_f0_frames:.4f}")


if __name__ == '__main__':
    main()