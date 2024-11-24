import os
import gc
import sys
import cv2
import json
import h5py
import yaml
import torch
import logging
from tqdm import tqdm
from torch.utils.data import DataLoader
from torch.optim import (
    AdamW
)
availabel_optimizers = {
    'AdamW': AdamW
}

# Setup base directory and add file to python path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '../'))

# PoinTr imports
from utils import misc
from tools import builder
from tools import training
from utils.config import cfg_from_yaml_file,  get_instance
from extensions.chamfer_dist import (
    ChamferDistanceL1, 
    ChamferDistanceL2
)
from pointnet2_ops import pointnet2_utils

# MVP Dataset  
from mvp.mvp_dataset import MVPDataset

# Clifford Algebra
from clifford_lib.algebra.cliffordalgebra import CliffordAlgebra

# Models
from models.ga_models.GAPoinTr import GAFeatures




def main():
    # Here insert argparser and select between train and test

    # Load training configuration file
    training_config = os.path.join(BASE_DIR, 'cfgs/GAPoinTr-training.yaml')
    with open(training_config, "r") as file:
        config = yaml.safe_load(file)
    
    # Setup logging
    os.makedirs(os.path.join(BASE_DIR , "logs"), exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(BASE_DIR, "logs/train.log"), 
        encoding="utf-8", 
        level=logging.DEBUG, 
        filemode="w"
    )
    logger = logging.getLogger(__name__)

    # Setup device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logger.info(f"device: {device}")

    # Build MVP dataset
    print("\nBuilding MVP Dataset...")
    # data_path = BASE_DIR + "/../mvp/datasets/"
    # logger.info(f"Data directory: {data_path}")
    train_data_path = config['train_dataset'] #data_path + "MVP_Train_CP.h5"
    load_train_dataset = h5py.File(train_data_path, 'r')
    train_dataset = MVPDataset(load_train_dataset, logger=logger)
    logger.info(f"lenght of train dataset: {len(train_dataset)}")
    print((f"Lenght of train dataset: {len(train_dataset)}"))
    test_data_path = config['test_dataset'] #data_path + "MVP_Test_CP.h5"
    load_test_dataset = h5py.File(test_data_path, 'r')
    test_dataset = MVPDataset(load_test_dataset, logger=logger)
    logger.info(f"lenght of test dataset: {len(test_dataset)}")

    # Make dataloader
    train_dataloader = DataLoader(train_dataset, batch_size=config['batch_size'], shuffle=True)
    test_dataloader = DataLoader(test_dataset, batch_size=config['batch_size'])
    logger.info(f"Number of training batches: {len(train_dataloader)}")
    print(f"Number of training batches: {len(train_dataloader)}")
    print("done")

    # Build algebra
    points = train_dataset[42][0] # get partial pcd to build the algebra
    logger.info(f"shape of a single partial pointcloud: {points[0].shape}")
    algebra_dim = int(points.shape[1])
    metric = [1 for i in range(algebra_dim)]
    print("\nGenerating the algebra...")
    algebra = CliffordAlgebra(metric)
    print(f"algebra dimention: \t {algebra.dim}")
    print(f"multivectors elements: \t {sum(algebra.subspaces)}")
    print(f"number of subspaces: \t {algebra.n_subspaces}")
    print(f"subspaces grades: \t {algebra.grades.tolist()}")
    print(f"subspaces dimentions: \t {algebra.subspaces.tolist()}")
    print("done")

    # Define PoinTr instance
    print("\nBuilding PoinTr...")
    pointr_init_config = os.path.join(
        BASE_DIR, "cfgs", config['pointr_config'], "PoinTr.yaml"
    )
    pointr_ckp = os.path.join(BASE_DIR, "ckpts", config['pointr_config'], "pointr.pth")
    pointr_config = cfg_from_yaml_file(pointr_init_config, root=BASE_DIR+"/")
    pointr = builder.model_builder(pointr_config.model)
    builder.load_model(pointr, pointr_ckp)
    pointr = pointr.to(device)

    # Define custom model
    print("\nBuilding GAPoinTr...")
    model = GAFeatures(
        algebra=algebra,  
        embed_dim=config['embed_dim'],
        hidden_dim=256,
        pointr=pointr
    )
    model = model.to(device)
    param_device = next(model.parameters()).device
    logger.info(f"model parameters device: {param_device}") 

    # GAPoinTr parametr estimation
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    param_size_bytes = total_params * 4  # Assuming float32
    model_size_mb = param_size_bytes / (1024 ** 2)
    print(f"Total Parameters: {total_params}")
    print(f"Trainable Parameters: {trainable_params}")
    print(f"Model Size: {model_size_mb:.2f} MB")
    print("done")

    # Build parameters from yaml
    parameters = {
        'epochs': config['epochs'],
        'optimizer': get_instance(
                        config['optimizer'], 
                        availabel_optimizers,
                        {"params": model.parameters()}
                    ),
        'device': device,
        'losses':  {
            'ChDL1': ChamferDistanceL1(),
            'ChDL2': ChamferDistanceL2()
        }
    }

    # Training
    print("\nTraining")
    trainer = training.Trainer(
        parameters=parameters,
        algebra=algebra,
        logger=logger,
        debug=config['debug']
    )
    trainer.train(
        backbone=pointr,
        model=model,
        dataloader=train_dataloader
    )
    print("End of training!")

    # Saving path
    save_dir = os.path.join(
        BASE_DIR, '..', 
        config['save_path'], 
        config['pointr_config'],
    )
    trainer.test(
        backbone=pointr,
        model=model,
        dataloader=test_dataloader
    )
    print(f"Test loss: {trainer.test_loss:5f}")

    # Saving train result
    run_counter = 0
    for file in os.listdir(save_dir):
        if config['run_name'].split('_') == file.split('_')[:-1]: 
            run_counter += 1
    if config['override_cache']and run_counter > 0: run_counter -= 1
    save_dir = os.path.join(save_dir, config['run_name'].lower())
    save_dir = save_dir+"_"+str(run_counter)
    os.makedirs(save_dir, exist_ok=True)

    # Saving
    save_file = os.path.join(save_dir, "model_state_dict.pt")
    save_loss = os.path.join(save_dir, "train_losses.json")
    logger.info(f"\nSaving checkpoints in: {save_dir}")
    logger.info(f"\nSaving losses in: {save_dir}")
    if not config['debug']:
        print(f"\nSaving checkpoints in: {save_dir}")
        torch.save(
            model.state_dict(), 
            save_file
        )
        with open(save_loss, "w") as l_file:
            json.dump(trainer.loss_trend, l_file, indent=4)

 
if __name__ == "__main__":
    main()