S2VFLM

## Environments

python 3.7

pytorch-gpu 1.0.1+cuda101

tensorflow-gpu 1.13.1

## Data

Data: You can download the dataset CUBird and NABird from following link：

https://drive.google.com/open?id=1NJDqvTrO3bDEWpNCyIE8NC_wdvAofIqE

Put the uncompressed data to the folder "data", like this:

S2VFLM

- data
  - CUB2011
  - NABidr

## Reproduce results

CUBird SCS mode && SCE mode

```
# run CUBird with SCS mode
python train_CUB.py --splitmode easy --margin 0.1 --confidence 0.5 --txt_feat_path data/CUB2011/CUB_TFIDF_top4sim.mat

# run CUBird with SCE mode
python train_CUB.py --splitmode hard --margin 0.1 --confidence 0.7 --txt_feat_path data/CUB2011/CUB_TFIDF_top1sim.mat
```

NABird SCS mode && SCE mode

```
# run NABird with SCS mode
python train_NAB.py --splitmode easy --margin 0.2 --confidence 0.6 --txt_feat_path data/NABird/NAB_TFIDF_top3sim.mat

# run NABird with SCE mode
python train_NAB.py --splitmode hard --margin 0.1 --confidence 0.4 --txt_feat_path data/NABird/NAB_TFIDF_top1sim.mat
```

