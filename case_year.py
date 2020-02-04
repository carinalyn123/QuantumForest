import os, sys
import time
sys.path.insert(0, './python-package/')
isMORT = len(sys.argv)>1 and sys.argv[1] == "mort"
if isMORT:
    sys.path.insert(1, 'E:/LiteMORT/python-package/')
    import litemort
    from litemort import *
    print(f"litemort={litemort.__version__}")
import numpy as np
import matplotlib.pyplot as plt
import node_lib
import pandas as pd
import pickle
import torch, torch.nn as nn
import torch.nn.functional as F
import lightgbm as lgb
import random
device = 'cuda' if torch.cuda.is_available() else 'cpu'

experiment_name = 'year_node_shallow'
experiment_name = '{}_{}.{:0>2d}.{:0>2d}_{:0>2d}_{:0>2d}'.format(experiment_name, *time.gmtime()[:5])
print("experiment:", experiment_name)

def LoadData(data_name="YEAR"):
    pkl_path = f'./data/{data_name}.pickle'
    if os.path.isfile(pkl_path):
        print("====== LoadData@{} ......".format(pkl_path))
        with open(pkl_path, "rb") as fp:
            data = pickle.load(fp)
    else:
        data = node_lib.Dataset(data_name, random_state=1337, quantile_transform=True, quantile_noise=1e-3)
        #data = node_lib.Dataset("HIGGS",data_path="F:/Datasets/",random_state=1337, quantile_transform=True, quantile_noise=1e-3)
        in_features = data.X_train.shape[1]
        mu, std = data.y_train.mean(), data.y_train.std()
        normalize = lambda x: ((x - mu) / std).astype(np.float32)
        data.y_train, data.y_valid, data.y_test = map(normalize, [data.y_train, data.y_valid, data.y_test])
        print("mean = %.5f, std = %.5f" % (mu, std))
        with open(pkl_path, "wb") as fp:
            pickle.dump(data,fp)
    return data

def GBDT_test(data):
    model_type = "mort" if isMORT else "lgb"
    some_rows = 10000
    early_stop = 100;    verbose_eval = 20
    metric = 'l2'       #"rmse"
    num_rounds = 10000; nLeaf = 41
    lr = 0.1;    bf = 0.51;    ff = 0.81

    params = {"objective": "regression", "metric": metric,
              "num_leaves": nLeaf, "learning_rate": lr, 'n_estimators': num_rounds,
              "bagging_freq": 1, "bagging_fraction": bf, "feature_fraction": ff, 'min_data_in_leaf': 10000,
              'verbose_eval': verbose_eval, "early_stopping_rounds": early_stop, 'n_jobs': -1, "elitism": 0
              }
    X_train, y_train = data.X_train, data.y_train
    X_valid, y_valid = data.X_valid, data.y_valid
    X_test, y_test = data.X_test, data.y_test
    if not np.isfortran(X_train):   #Very important!!! mort need COLUMN-MAJOR format
        X_train = np.asfortranarray(X_train)
        X_valid = np.asfortranarray(X_valid)
    #X_train, X_valid = pd.DataFrame(X_train), pd.DataFrame(X_valid)
    print(f"GBDT_test\ttrain={X_train.shape} valid={X_valid.shape} test={X_test.shape}")
    #print(f"X_train=\n{X_train.head()}\n{X_train.tail()}")
    if model_type == 'mort':
        params['verbose'] = 667
        model = LiteMORT(params).fit(X_train, y_train, eval_set=[(X_valid, y_valid)])
        #y_pred_valid = model.predict(X_valid)
        #y_pred = model.predict(X_test)

    if model_type == 'lgb':
        params['verbose'] = 667
        model = lgb.LGBMRegressor(**params)
        model.fit(X_train, y_train,eval_set=[(X_train, y_train), (X_valid, y_valid)],verbose=200)
        #model.booster_.save_model('geo_test_.model')

def NODE_test(data):
    depth,batch_size=5,256          #6,1024
    in_features = data.X_train.shape[1]
    model = nn.Sequential(
        node_lib.DenseBlock(in_features, 2048, num_layers=1, tree_dim=3, depth=depth, flatten_output=False,
                       choice_function=node_lib.entmax15, bin_function=node_lib.entmoid15),
        node_lib.Lambda(lambda x: x[..., 0].mean(dim=-1)),  # average first channels of every tree
    ).to(device)

    def dump_model_params(model):
        nzParams = 0
        for name, param in model.named_parameters():
            if param.requires_grad:
                nzParams += param.nelement()
                print(f"\t{name}={param.nelement()}")
        print(f"========All parameters={nzParams}")
        return nzParams
    print(model)
    dump_model_params(model)

    if False:       # trigger data-aware init
        with torch.no_grad():
            res = model(torch.as_tensor(data.X_train[:1000], device=device))

    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)

    from qhoptim.pyt import QHAdam
    optimizer_params = { 'nus':(0.7, 1.0), 'betas':(0.95, 0.998) }
    trainer = node_lib.Trainer(
        model=model, loss_function=F.mse_loss,
        experiment_name=experiment_name,
        warm_start=False,
        Optimizer=QHAdam,
        optimizer_params=optimizer_params,
        verbose=True,
        n_last_checkpoints=5
    )
    from tqdm import tqdm
    from IPython.display import clear_output
    loss_history, mse_history = [], []
    best_mse = float('inf')
    best_step_mse = 0
    early_stopping_rounds = 5000
    report_frequency = 1000

    print(f"trainer.model={trainer.model}\ntrainer.loss_function={trainer.loss_function}\ntrainer.opt={trainer.opt}")
    t0=time.time()
    for batch in node_lib.iterate_minibatches(data.X_train, data.y_train, batch_size=batch_size,
                                         shuffle=True, epochs=float('inf')):
        metrics = trainer.train_on_batch(*batch, device=device)
        loss_history.append(metrics['loss'])
        print(f"\r============ {trainer.step}\tLoss={metrics['loss']:.5f}\ttime={time.time()-t0:.6f}",end="")
        if trainer.step % report_frequency == 0:
            trainer.save_checkpoint()
            trainer.average_checkpoints(out_tag='avg')
            trainer.load_checkpoint(tag='avg')
            mse = trainer.evaluate_mse(
                data.X_valid, data.y_valid, device=device, batch_size=1024)

            if mse < best_mse:
                best_mse = mse
                best_step_mse = trainer.step
                trainer.save_checkpoint(tag='best_mse')
            mse_history.append(mse)

            trainer.load_checkpoint()  # last
            trainer.remove_old_temp_checkpoints()

            clear_output(True)
            plt.figure(figsize=[18, 6])
            plt.subplot(1, 2, 1)
            plt.plot(loss_history)
            plt.title('Loss')
            plt.grid()
            plt.subplot(1, 2, 2)
            plt.plot(mse_history)
            plt.title('MSE')
            plt.grid()
            plt.show()
            print(f"loss_{trainer.step}\t{metrics['loss']:.5f}\tVal MSE:{mse:.5f}" )
        if trainer.step > best_step_mse + early_stopping_rounds:
            print('BREAK. There is no improvment for {} steps'.format(early_stopping_rounds))
            print("Best step: ", best_step_mse)
            print("Best Val MSE: %0.5f" % (best_mse))
            break

    trainer.load_checkpoint(tag='best_mse')
    mse = trainer.evaluate_mse(data.X_test, data.y_test, device=device)
    print('Best step: ', trainer.step)
    print("Test MSE: %0.5f" % (mse))

if __name__ == "__main__":
    data = LoadData()
    random_state = 42
    np.random.seed(random_state)
    torch.manual_seed(random_state)
    random.seed(random_state)
    if True:
        NODE_test(data)
    else:
        GBDT_test(data)
        input("...")