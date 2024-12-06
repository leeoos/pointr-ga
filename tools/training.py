import os
import gc
import sys
import cv2
import json
import h5py
import yaml
import torch
import logging
from pprint import pprint
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
from utils.config import (
    cfg_from_yaml_file,  
    get_instance, 
    dump_all_modules_parameters
)
from extensions.chamfer_dist import (
    ChamferDistanceL1, 
    ChamferDistanceL2
)
from pointnet2_ops import pointnet2_utils

# # MVP Dataset  
# from mvp.mvp_dataset import MVPDataset

# Clifford Algebra
from clifford_lib.algebra.cliffordalgebra import CliffordAlgebra


class Trainer():

    def __init__(self, parameters, logger, cfg, dump_file='' ) -> None:
        self.parameters = parameters
        self.logger = logger
        self.debug = cfg['debug']
        self.progressive = cfg['progressive_saves']
        self.save_step = cfg['save_step']
        self.dump_file = dump_file
        self.loss_trend = {}
        self.test_loss = 0 

    def train(self, model, dataloader, save_path) -> None:

        flush_step = 500
        epoch_loss = None
        device = self.parameters.get('device', None)
        train_epochs = self.parameters.get('epochs', None)
        optimizer = self.parameters.get('optimizer', None)
        scheduler = self.parameters.get('scheduler', None)
        loss_fn = model.loss_fn

        # Ensure device
        model = model.to(device)

        # Info
        print("\nTrain Loss Function:")
        pprint(loss_fn)
        print("\nOptimizer:")
        pprint(optimizer)
    
        with tqdm(total=train_epochs, leave=True, disable=self.debug) as pbar:
            for epoch in range(train_epochs):
                epoch_loss = 0
                with tqdm(total=len(dataloader), disable=self.debug) as bbar:
                    for step, pcd in enumerate(dataloader):

                        # Get samples and reset optimizer
                        partial, complete = pcd
                        optimizer.zero_grad()

                        # Send point clouds to device
                        partial = partial.to(device)
                        complete = complete.to(torch.float32).to(device)
                        output = model(partial)

                        # Backward step
                        loss = loss_fn(output, complete)
                        loss.backward()
                        optimizer.step()
                        epoch_loss += loss.item()

                        if self.debug: print(f"loss: {loss.item()}")
                        bbar.set_postfix(batch_loss=loss.item())
                        bbar.update(1)
                        self.logger.info(f"batch loss: {loss.item()}\tepoch: {(epoch + 1) // (step + 1)}")

                        # Collect epoch losses for statistic
                        epoch_key = "epoch" + "_" + str(epoch)
                        if  epoch_key not in self.loss_trend.keys():
                            self.loss_trend[epoch_key] = [loss.item()]
                        else:
                            self.loss_trend[epoch_key].append(loss.item())

                        # Save and flush
                        if step > 0 and step % self.save_step == 0 and\
                            self.progressive and not self.debug:
                                checkpoint = { 
                                    'epoch': epoch,
                                    'model': model.state_dict(),
                                    'optimizer': optimizer.state_dict(),
                                }
                                save_dir=  os.path.join(
                                    save_path, f"training/{str(step*(epoch+1))}"
                                )
                                os.makedirs(save_dir, exist_ok=True)
                                save_file = os.path.join(save_dir, "checkpoint.pt")
                                torch.save(checkpoint, save_file)
                        
                        # Free up cuda mem
                        del complete
                        del partial
                        del output
                        del loss

                        # Flush
                        if step > 0 and step % flush_step == 0:
                            # Flush
                            gc.collect()
                            torch.cuda.empty_cache()
                            gc.collect()

                        if self.debug: break

                if scheduler: scheduler.step()
                epoch_loss = epoch_loss/(step + 1)
                pbar.set_postfix(epoch_train=epoch_loss)
                pbar.update(1)
                if self.debug: break

            # Final save
            if not self.debug:
                checkpoint = { 
                    'epoch': epoch,
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                }
                save_dir=  os.path.join(
                    save_path, "training/final"
                )
                os.makedirs(save_dir, exist_ok=True)
                save_file = os.path.join(save_dir, "checkpoint.pt")
                torch.save(checkpoint, save_file)
                save_loss = os.path.join(save_dir, "train_losses.json")
                with open(save_loss, "w") as l_file:
                    json.dump(self.loss_trend, l_file, indent=4)


            # Dump model state after training
            if self.dump_file:
                dump_all_modules_parameters(model, self.dump_file)
                

    def test(self, model, dataloader, save_path):

        flush_step = 500
        batch_loss = 0
        device = self.parameters['device']
        loss_fn = model.test_loss

        model = model.to(device)
        model.eval()

        print("\Test Loss Function:")
        pprint(loss_fn)

        with tqdm(total=len(dataloader), disable=self.debug) as bbar:
            for step, pcd in enumerate(dataloader):

                partial, complete = pcd

                # Send point clouds to device
                partial = partial.to(device)
                complete = complete.to(torch.float32).to(device)

                # Pass partial pcd to PoinTr
                with torch.no_grad():
                    output = model(partial)


                loss = loss_fn(output, complete)
                batch_loss += loss.item()

                if self.debug:  print(f"loss: {loss.item()}")

                bbar.set_postfix(batch_loss=loss.item())
                bbar.update(1)
                self.logger.info(f"train loss: {loss.item()}")

                # free up cuda mem
                del complete
                del partial
                del output
                del loss
                if step > 0 and step % flush_step == 0:
                    gc.collect()
                    torch.cuda.empty_cache()
                    gc.collect()

                if self.debug: break

            self.test_loss = batch_loss/(step + 1)

        if not self.debug:
            save_dir = os.path.join(save_path, "evaluation/")
            os.makedirs(save_dir, exist_ok=True)
            with open(os.path.join(save_dir, "test_loss.txt"), "w") as file:
                file.write(f"Test Loss: {str((self.test_loss))}")
            