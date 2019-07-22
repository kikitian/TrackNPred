from ignite.engine import Engine, Events
from model.Prediction.utils import lstToCuda,maskedNLL,maskedMSE,maskedNLLTest
import time
import math
import torch
from ignite.contrib.handlers import ProgressBar
import os
import datetime

class TrajPredEngine:

    def __init__(self, net, optim, train_loader, val_loader, args, thread = None):
        self.net = net
        self.args = args
        self.pretrainEpochs = args["pretrainEpochs"]
        self.trainEpochs = args["trainEpochs"]
        self.optim = optim
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.cuda = args['cuda']
        # self.cuda = False
        self.eval_only = args['eval']
        self.thread = thread

        self.n_iterations = max(len(train_loader), len(train_loader) / args["batch_size"])

        ## training metrics to keep track of, consider making a metrics class
        # remember to 0 these out
        self.avg_trn_loss = 0

        self.metrics = {"Avg train loss": 0, "Avg val loss": 0 }
        ## validation metrics
        self.avg_val_loss = 0
        self.val_batch_count = 1

        # only if using maneuvers
        self.avg_lat_acc = 0
        self.avg_lon_acc = 0

        self.trainer = None
        self.evaluator = None

        self.makeTrainer()

        self.save_name = args['name']

    def netPred(self, batch):
        raise NotImplementedError

    def saveModel(self, engine):
        save_dir = "model/Prediction/trained_models/"
        os.makedirs(save_dir, exist_ok=True)
        name = os.path.join(save_dir, self.args['name'])
        torch.save(self.net.state_dict(), name)
        print("Model saved {}.".format(name))

    def train_batch(self, engine, batch):

        self.net.train_flag = True
        epoch = engine.state.epoch

        _, _, _, _, _, _, _, fut, op_mask = batch

        fut_pred = self.netPred(batch)

        if self.cuda:
            fut = fut.cuda()
            op_mask = op_mask.cuda()

        if epoch < self.pretrainEpochs:
            if self.args["pretrain_loss"] == 'MSE':
                l = maskedMSE(fut_pred, fut, op_mask)
            elif self.args['pretrain_loss'] == 'NLL':
                l = maskedNLL(fut_pred, fut, op_mask)
            else:
                self.thread.signalError("[Error] Unrecognized pretrain loss, using MSE by default")
                l = maskedMSE(fut_pred, fut, op_mask)
        else:
            if self.args["train_loss"] == 'MSE':
                l = maskedMSE(fut_pred, fut, op_mask)
            elif self.args['train_loss'] == 'NLL':
                l = maskedNLL(fut_pred, fut, op_mask)
            else:
                self.thread.signalError("[Error] Unrecognized train loss, using NLL by default")
                l = maskedNLL(fut_pred, fut, op_mask)

        # if self.args['nll_only']:
        #     l = maskedNLL(fut_pred, fut, op_mask)
        # else:
        #     if epoch < self.pretrainEpochs:
        #         l = maskedMSE(fut_pred, fut, op_mask)
        #     else:
        #         l = maskedNLL(fut_pred, fut, op_mask)

        # Backprop and update weights
        self.optim.zero_grad()
        l.backward()
        self.optim.step()

        # Track average train loss:
        self.avg_trn_loss += l.item()
        self.metrics["Avg train loss"] += l.item() / 100.0
        print(self.metrics["Avg train loss"])

        return l.item()

    def eval_batch(self, engine, batch):
        self.net.train_flag = False


        epoch = engine.state.epoch

        _, _, _, _, _, _, _, fut, op_mask = batch
        fut_pred = self.netPred(batch)
        if self.cuda:
            fut = fut.cuda()
            op_mask = op_mask.cuda()

        # Forward pass

        if epoch < self.pretrainEpochs:
            if self.args["pretrain_loss"] == 'MSE':
                l = maskedMSE(fut_pred, fut, op_mask)
            elif self.args['pretrain_loss'] == 'NLL':
                l = maskedNLL(fut_pred, fut, op_mask)
            else:
                self.thread.signalError("[Error] Unrecognized pretrain loss, using MSE by default")
                l = maskedMSE(fut_pred, fut, op_mask)
        else:
            if self.args["train_loss"] == 'MSE':
                l = maskedMSE(fut_pred, fut, op_mask)
            elif self.args['train_loss'] == 'NLL':
                l = maskedNLL(fut_pred, fut, op_mask)
            else:
                self.thread.signalError("[Error] Unrecognized train loss, using NLL by default")
                l = maskedNLL(fut_pred, fut, op_mask)


        # if self.args['nll_only']:
        #     l = maskedNLL(fut_pred, fut, op_mask)
        # else:
        #     if epoch_num < pretrainEpochs:
        #         l = maskedMSE(fut_pred, fut, op_mask)
        #     else:
        #         l = maskedNLL(fut_pred, fut, op_mask)

        self.avg_val_loss += l.item()
        self.metrics["Avg val loss"] += l.item()/ (self.val_batch_count * 100.0)
        self.val_batch_count += 1

        return fut_pred, fut

    def validate(self, engine):
        self.evaluator.run(self.val_loader)
        max_epochs =self.args["pretrainEpochs"] + self.args["trainEpochs"]
        if self.thread:
            if not self.eval_only:
                self.thread.signalBotLabel("{}/{} Epochs".format(engine.state.epoch, max_epochs))
                self.thread.signalBotBar(max((engine.state.epoch / max_epochs) * 100,1))
                self.thread.signalCanvas("\nEPOCH {}: Train loss: {}  Val loss: {}".format(engine.state.epoch, self.metrics["Avg train loss"], self.metrics["Avg val loss"]))
            else:
                self.thread.signalCanvas("\nEPOCH {}: Test loss: {}".format(engine.state.epoch, self.metrics["Avg val loss"]))
        else:
            if not self.eval_only:
                print("{}/{} Epochs".format(engine.state.epoch, max_epochs))
                print(max((engine.state.epoch / max_epochs) * 100,1))
                print("\nEPOCH {}: Train loss: {}  Val loss: {}".format(engine.state.epoch, self.metrics["Avg train loss"], self.metrics["Avg val loss"]))
            else:
                print("\nEPOCH {}: Test loss: {}".format(engine.state.epoch, self.metrics["Avg val loss"]))


        self.metrics["Avg train loss"] = 0

    def zeroMetrics(self, engine):
        self.val_batch_count = 1
        if self.thread and not self.eval_only:
            self.thread.signalTopLabel("{}/{} Iterations".format(engine.state.iteration % self.n_iterations, self.n_iterations))
            self.thread.signalTopBar(( (engine.state.iteration % self.n_iterations) / self.n_iterations ) * 100)
        self.metrics["Avg val loss"] = 0 

    def zeroTrainLoss(self, engine):
        self.metrics["Avg train loss"] = 0

    def zeroValLoss(self, engine):
        self.metrics["Avg val loss"] = 0

    def makeTrainer(self):
        self.trainer = Engine(self.train_batch)
        self.evaluator = Engine(self.eval_batch)

        # pbar = ProgressBar(persist=True, postfix=self.metrics)
        # pbar.attach(self.trainer)

        ## attach hooks 
        # evaluate after every batch
        # if not self.eval_only:
        self.trainer.add_event_handler(Events.EPOCH_COMPLETED, self.validate)
        self.trainer.add_event_handler(Events.ITERATION_COMPLETED, self.zeroMetrics)
        self.trainer.add_event_handler(Events.COMPLETED, self.saveModel)
        # zero out metrics for next epoch


    def start(self):
        max_epochs =self.args["pretrainEpochs"] + self.args["trainEpochs"]
        if self.thread:
            self.thread.signalBotLabel("0/{} Epochs".format(max_epochs))
            self.thread.signalTopLabel("0/{} Iterations".format(self.n_iterations))

        if not self.eval_only:
            self.trainer.run(self.train_loader, max_epochs=max_epochs)
        else:
            self.trainer.run(self.train_loader, max_epochs=1)


    def eval(self):
        if self.thread:
            self.thread.signalTopLabel("Evaluating")

        evaluator = Engine(self.eval_batch)



