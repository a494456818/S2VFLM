import torch
import torch.nn.functional as F
from torch.autograd import Variable
import torch.autograd as autograd
import torch.optim as optim
import torch.nn.init as init

from sklearn.metrics.pairwise import cosine_similarity
# from sklearn.metrics.pairwise import euclidean_distances as cosine_similarity
import scipy.integrate as integrate
from termcolor import cprint
from time import gmtime, strftime
import numpy as np
import argparse
import os
import random
import glob
import copy 
import json
from dataset import FeatDataLayer, LoadDataset
from models_1 import _netD, _netG, _param
import tensorflow as tf
from unsupervised_dataset import UnsupervisedData
from sklearn.neighbors import KNeighborsClassifier


parser = argparse.ArgumentParser()
parser.add_argument('--gpu', default='0', type=str, help='index of GPU to use')
parser.add_argument('--splitmode', default='easy', type=str, help='the way to split train/test data: easy/hard')
parser.add_argument('--manualSeed', type=int, help='manual seed')
parser.add_argument('--resume',  type=str, help='the model to resume', default=None)
parser.add_argument('--disp_interval', type=int, default=20)
parser.add_argument('--save_interval', type=int, default=200)
parser.add_argument('--evl_interval',  type=int, default=40)
parser.add_argument('--txt_feat_path',  type=str, default="")
parser.add_argument('--margin',  type=float, default="")
parser.add_argument('--confidence',  type=float, default="")



opt = parser.parse_args()
print('Running parameters:')
print(json.dumps(vars(opt), indent=4, separators=(',', ':')))

os.environ['CUDA_VISIBLE_DEVICES'] = opt.gpu


""" hyper-parameter for training """
opt.GP_LAMBDA = 10      # Gradient penalty lambda
opt.CENT_LAMBDA  = 1
opt.REG_W_LAMBDA = 0.001
opt.REG_Wz_LAMBDA = 0.0001

opt.lr = 0.0001
opt.batchsize = 1000


""" hyper-parameter for testing"""
opt.nSample = 60  # number of fake feature for each class
opt.Knn = 60      # knn: the value of K

if opt.manualSeed is None:
    opt.manualSeed = random.randint(1, 10000)
print("Random Seed: ", opt.manualSeed)
random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)
torch.cuda.manual_seed_all(opt.manualSeed)

def cal_triplets_loss(anchor, train_dic, margin):
    _len = anchor.shape[0]
    negative_loss = Variable(torch.Tensor([0.0])).cuda()
    pos_loss = Variable(torch.Tensor([0.0])).cuda()
    for i in range(_len):
        # positive
        positive = Variable(torch.from_numpy(np.array(train_dic[i]))).cuda()
        pos_dist = torch.mean(torch.sqrt(torch.sum(torch.pow(positive.sub_(anchor[i]),2),1)))
        pos_loss = torch.add(pos_loss, pos_dist)
        # negative
        other_cls = list(set(train_dic.keys()) - set([i]))
        for j in other_cls:
            negative = Variable(torch.from_numpy(random.sample(train_dic[j], 1)[0])).cuda()
            neg_dist = torch.sqrt(torch.sum(torch.pow(negative.sub_(anchor[i]), 2)))
            negative_loss = torch.add(negative_loss, neg_dist)
        negative_loss = torch.div(negative_loss, Variable(torch.Tensor([len(other_cls)])).cuda())
    pos_loss = torch.div(pos_loss, Variable(torch.Tensor([_len])).cuda())
    basic_loss = torch.add(torch.sub(pos_loss, negative_loss), margin)
    if basic_loss < 0:
        basic_loss = Variable(torch.Tensor([0.0])).cuda()
    loss = 1/2 * basic_loss
    return loss

def train():
    param = _param()
    dataset = LoadDataset(opt)
    param.X_dim = dataset.feature_dim

    data_layer = FeatDataLayer(dataset.labels_train, dataset.pfc_feat_data_train, opt)
    result = Result()
    result_gzsl = Result()
    netG = _netG(dataset.text_dim, dataset.feature_dim).cuda()
    netG.apply(weights_init)
    print(netG)
    netD = _netD(dataset.train_cls_num, dataset.feature_dim).cuda()
    netD.apply(weights_init)
    print(netD)

    exp_info = 'CUB_EASY' if opt.splitmode == 'easy' else 'CUB_HARD'
    exp_params = 'Eu{}_Rls{}_RWz{}'.format(opt.CENT_LAMBDA , opt.REG_W_LAMBDA, opt.REG_Wz_LAMBDA)

    train_dic = {}
    for i in range(len(dataset.labels_train)):
        try:
            train_dic[dataset.labels_train[i]].append(dataset.pfc_feat_data_train[i])
        except:
            train_dic[dataset.labels_train[i]] = [dataset.pfc_feat_data_train[i]]

    out_dir = 'out/{:s}'.format(exp_info)
    out_subdir = 'out/{:s}/{:s}'.format(exp_info, exp_params)
    if not os.path.exists('out'):
        os.mkdir('out')
    if not os.path.exists(out_dir):
        os.mkdir(out_dir)
    if not os.path.exists(out_subdir):
        os.mkdir(out_subdir)

    cprint(" The output dictionary is {}".format(out_subdir), 'red')
    log_dir  = out_subdir + '/log_{:s}.txt'.format(exp_info)
    with open(log_dir, 'a') as f:
        f.write('Training Start:')
        f.write(strftime("%a, %d %b %Y %H:%M:%S +0000", gmtime()) + '\n')

    if opt.resume:
        if os.path.isfile(opt.resume):
            print("=> loading checkpoint '{}'".format(opt.resume))
            checkpoint = torch.load(opt.resume)
            netG.load_state_dict(checkpoint['state_dict_G'])
            netD.load_state_dict(checkpoint['state_dict_D'])
            start_step = checkpoint['it']
            print(checkpoint['log'])
        else:
            print("=> no checkpoint found at '{}'".format(opt.resume))

    nets = [netG, netD]

    tr_cls_centroid = Variable(torch.from_numpy(dataset.tr_cls_centroid.astype('float32'))).cuda()
    optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(0.5, 0.9))
    optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(0.5, 0.9))
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    unsupervisedData = UnsupervisedData(dataset.test_text_feature, dataset.labels_test,
                                        dataset.pfc_feat_data_test, dataset.train_cls_num)

    first = True if opt.resume != None else False
    class_increment = False

    while True:
        if not first:
            start_step = 0
            class_increment = False
            for it in range(start_step, 3000+1):
                """ Discriminator """
                for _ in range(5):
                    blobs = data_layer.forward()
                    feat_data = blobs['data']             # image data
                    labels = blobs['labels'].astype(int)  # class labels
                    text_feat = np.array([dataset.train_text_feature[i,:] for i in labels])
                    text_feat = Variable(torch.from_numpy(text_feat.astype('float32'))).cuda()
                    np.unique(labels)
                    X = Variable(torch.from_numpy(feat_data)).cuda()
                    y_true = Variable(torch.from_numpy(labels.astype('int'))).cuda()
                    z = Variable(torch.randn(opt.batchsize, param.z_dim)).cuda()
                    y_true = y_true.to(device=device, dtype=torch.long)

                    # GAN's D loss
                    D_real, C_real = netD(X)
                    D_loss_real = torch.mean(D_real)
                    # print(C_real)
                    # print(y_true)
                    C_loss_real = F.cross_entropy(C_real, y_true)
                    DC_loss = -D_loss_real + C_loss_real
                    DC_loss.backward()

                    # GAN's D loss
                    G_sample = netG(z, text_feat).detach()
                    D_fake, C_fake = netD(G_sample)
                    D_loss_fake = torch.mean(D_fake)
                    C_loss_fake = F.cross_entropy(C_fake, y_true)
                    DC_loss = D_loss_fake + C_loss_fake
                    DC_loss.backward()

                    # train with gradient penalty (WGAN_GP)
                    grad_penalty = calc_gradient_penalty(netD, X.data, G_sample.data)
                    grad_penalty.backward()

                    Wasserstein_D = D_loss_real - D_loss_fake
                    optimizerD.step()
                    reset_grad(nets)

                """ Generator """
                for _ in range(1):
                    blobs = data_layer.forward()
                    feat_data = blobs['data']  # image data, 最小批的图片数据
                    labels = blobs['labels'].astype(int)  # class labels, 图片对应的标签
                    text_feat = np.array([dataset.train_text_feature[i, :] for i in labels])
                    text_feat = Variable(torch.from_numpy(text_feat.astype('float32'))).cuda() # 获取对应的文本
                    anchor_text_feat = Variable(torch.from_numpy(dataset.train_text_feature.astype('float32'))).cuda()

                    X = Variable(torch.from_numpy(feat_data)).cuda()
                    y_true = Variable(torch.from_numpy(labels.astype('int'))).cuda()
                    y_true = y_true.to(device=device, dtype=torch.long)
                    z = Variable(torch.randn(opt.batchsize, param.z_dim)).cuda()
                    anchor_z = Variable(torch.randn(len(dataset.train_text_feature), param.z_dim)).cuda()

                    G_sample = netG(z, text_feat)
                    D_fake, C_fake = netD(G_sample)
                    _,      C_real = netD(X)

                    # GAN's G loss
                    G_loss = torch.mean(D_fake)
                    # Auxiliary classification loss
                    C_loss = (F.cross_entropy(C_real, y_true) + F.cross_entropy(C_fake, y_true))/2

                    GC_loss = -G_loss + C_loss

                    # Centroid loss
                    Euclidean_loss_1 = Variable(torch.Tensor([0.0])).cuda()
                    Euclidean_loss_2 = Variable(torch.Tensor([0.0])).cuda()

                    if opt.CENT_LAMBDA != 0:
                        for i in range(dataset.train_cls_num):
                            sample_idx = (y_true == i).data.nonzero().squeeze()
                            try:
                                eq_idx_len = sample_idx.shape[0]
                            except:
                                eq_idx_len = 0
                            if sample_idx.numel() == 0:
                                Euclidean_loss_1 += 0.0
                            else:
                                G_sample_cls = G_sample[sample_idx, :]
                                Euclidean_loss_1 += (G_sample_cls.mean(dim=0) - tr_cls_centroid[i]).pow(2).sum().sqrt()

                            sample_idx = (y_true != i).data.nonzero().squeeze()
                            try:
                                sample_idx = random.sample(sample_idx, eq_idx_len)
                            except:
                                pass
                            if eq_idx_len == 0:
                                Euclidean_loss_2 += 0.0
                            else:
                                G_sample_cls = G_sample[sample_idx, :]
                                Euclidean_loss_2 += (G_sample_cls.mean(dim=0) - tr_cls_centroid[i]).pow(2).sum().sqrt()
                        Euclidean_loss_1 *= 1.0 / dataset.train_cls_num * opt.CENT_LAMBDA
                        Euclidean_loss_2 *= 1.0 / dataset.train_cls_num * opt.CENT_LAMBDA

                    Euclidean_loss = Euclidean_loss_1 - Euclidean_loss_2

                    # ||W||_2 regularization
                    reg_loss = Variable(torch.Tensor([0.0])).cuda()
                    if opt.REG_W_LAMBDA != 0:
                        for name, p in netG.named_parameters():
                            if 'weight' in name:
                                reg_loss += p.pow(2).sum()
                        reg_loss.mul_(opt.REG_W_LAMBDA)

                    # ||W_z||21 regularization, make W_z sparse
                    reg_Wz_loss = Variable(torch.Tensor([0.0])).cuda()
                    if opt.REG_Wz_LAMBDA != 0:
                        Wz = netG.rdc_text.weight
                        reg_Wz_loss = Wz.pow(2).sum(dim=0).sqrt().sum().mul(opt.REG_Wz_LAMBDA)

                    anchor = netG(anchor_z, anchor_text_feat)
                    triplet_loss = cal_triplets_loss(anchor, train_dic, opt.margin)

                    all_loss = GC_loss + Euclidean_loss + reg_loss + reg_Wz_loss + triplet_loss
                    all_loss.backward()
                    optimizerG.step()
                    reset_grad(nets)

                if it % opt.disp_interval == 0 and it:
                    acc_real = (np.argmax(C_real.data.cpu().numpy(), axis=1) == y_true.data.cpu().numpy()).sum() / float(y_true.data.size()[0])
                    acc_fake = (np.argmax(C_fake.data.cpu().numpy(), axis=1) == y_true.data.cpu().numpy()).sum() / float(y_true.data.size()[0])

                    log_text = 'Iter-{}; Was_D: {:.4}; Euc_triplet_ls: {:.4}; reg_ls: {:.4}; Wz_ls: {:.4}; G_loss: {:.4}; D_loss_real: {:.4};' \
                               ' D_loss_fake: {:.4}; rl: {:.4}%; fk: {:.4}%'\
                                .format(it,
                                        Wasserstein_D.item(),
                                        Euclidean_loss.item()+triplet_loss.item(),
                                        reg_loss.item(),
                                        reg_Wz_loss.item(),
                                        G_loss.item(),
                                        D_loss_real.item(),
                                        D_loss_fake.item(),
                                        acc_real * 100, acc_fake * 100)
                    print(log_text)
                    with open(log_dir, 'a') as f:
                        f.write(log_text+'\n')

                if it % opt.evl_interval == 0 and it >= 100:
                    netG.eval()
                    eval_fakefeat_test(it, netG, dataset, param, result)
                    eval_fakefeat_GZSL(it, netG, dataset, param, result_gzsl)
                    if result.save_model:
                        files2remove = glob.glob(out_subdir + '/Best_model*')
                        for _i in files2remove:
                            os.remove(_i)
                        torch.save({
                            'it': it + 1,
                            'state_dict_G': netG.state_dict(),
                            'state_dict_D': netD.state_dict(),
                            'random_seed': opt.manualSeed,
                            'log': log_text,
                        }, out_subdir + '/Best_model_Acc_{:.2f}.tar'.format(result.acc_list[-1]))
                    netG.train()

                if it % opt.save_interval == 0 and it:
                    torch.save({
                            'it': it + 1,
                            'state_dict_G': netG.state_dict(),
                            'state_dict_D': netD.state_dict(),
                            'random_seed': opt.manualSeed,
                            'log': log_text,
                        },  out_subdir + '/Iter_{:d}.tar'.format(it))
                    cprint('Save model to ' + out_subdir + '/Iter_{:d}.tar'.format(it), 'red')

        first = False

        # semi-supervised
        text_feat = Variable(torch.from_numpy(unsupervisedData.text_feature.astype('float32'))).cuda()
        z = Variable(torch.randn(text_feat.shape[0], param.z_dim)).cuda()
        text_feat = netG(z, text_feat).data.cpu().numpy()

        model = KNeighborsClassifier(50)
        model.fit(text_feat, unsupervisedData.labels)

        y_pro = model.predict_proba(unsupervisedData.image_feature)
        y = model.predict(unsupervisedData.image_feature)
        probabilities = y_pro[:, np.argsort(y_pro)[::, -1][0]]
        selectedHighConvinceIndex = list(np.where(probabilities >= opt.confidence)[0])
        selectedHighConvinceIndex_y = y[selectedHighConvinceIndex]
        print("select high confidence number : " + str(len(selectedHighConvinceIndex)))

        for i, label in enumerate(selectedHighConvinceIndex_y):

            if label in unsupervisedData.unsupervised_label_mapping:
                label = unsupervisedData.unsupervised_label_mapping[label]

                insertIndex = np.where(dataset.labels_train == label)[0][0]
                dataset.labels_train = np.insert(dataset.labels_train, insertIndex, values=label, axis=0)
                dataset.pfc_feat_data_train = np.insert(dataset.pfc_feat_data_train, insertIndex, values=unsupervisedData.image_feature[selectedHighConvinceIndex[i]], axis=0)
                train_dic[label].append(unsupervisedData.image_feature[selectedHighConvinceIndex[i]])

            else:
                unsupervisedData.unsupervised_label_mapping[label] = unsupervisedData.label_index
                unsupervisedData.label_index += 1
                label = unsupervisedData.unsupervised_label_mapping[label]


                dataset.labels_train = np.hstack([dataset.labels_train, [label]])
                dataset.pfc_feat_data_train = np.vstack(
                    [dataset.pfc_feat_data_train, [unsupervisedData.image_feature[selectedHighConvinceIndex[i]]]])
                dataset.train_text_feature = np.vstack(
                    [dataset.train_text_feature, [unsupervisedData.text_feature[selectedHighConvinceIndex[i]]]])
                train_dic[label] = [unsupervisedData.image_feature[selectedHighConvinceIndex[i]]]

                dataset.train_cls_num += 1
                class_increment = True

        unsupervisedData.text_feature = np.delete(unsupervisedData.text_feature, selectedHighConvinceIndex, axis=0)
        unsupervisedData.image_feature = np.delete(unsupervisedData.image_feature, selectedHighConvinceIndex, axis=0)
        unsupervisedData.labels = np.delete(unsupervisedData.labels, selectedHighConvinceIndex, axis=0)

        dataset.tr_cls_centroid = np.zeros([dataset.train_cls_num, dataset.pfc_feat_data_train.shape[1]]).astype(
            np.float32)
        for i in range(dataset.train_cls_num):
            dataset.tr_cls_centroid[i] = np.mean(dataset.pfc_feat_data_train[dataset.labels_train == i],
                                                 axis=0)
        tr_cls_centroid = Variable(torch.from_numpy(dataset.tr_cls_centroid.astype('float32'))).cuda()

        if class_increment:
            del netD
            netD = _netD(dataset.train_cls_num, dataset.feature_dim).cuda()
            netD.apply(weights_init)
            netG = _netG(dataset.text_dim, dataset.feature_dim).cuda()
            netG.apply(weights_init)

            optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(0.5, 0.9))
            optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(0.5, 0.9))

            nets = [netG, netD]

            print(netG)
            print(netD)
        data_layer = FeatDataLayer(dataset.labels_train, dataset.pfc_feat_data_train, opt)


def eval_fakefeat_test(it, netG, dataset, param, result):
    gen_feat = np.zeros([0, param.X_dim])
    for i in range(dataset.test_cls_num):
        text_feat = np.tile(dataset.test_text_feature[i].astype('float32'), (opt.nSample, 1))
        text_feat = Variable(torch.from_numpy(text_feat)).cuda()
        z = Variable(torch.randn(opt.nSample, param.z_dim)).cuda()
        G_sample = netG(z, text_feat)
        gen_feat = np.vstack((gen_feat, G_sample.data.cpu().numpy()))

    # cosince predict K-nearest Neighbor
    sim = cosine_similarity(dataset.pfc_feat_data_test, gen_feat)
    idx_mat = np.argsort(-1 * sim, axis=1)
    label_mat = (idx_mat[:, 0:opt.Knn] / opt.nSample).astype(int)
    preds = np.zeros(label_mat.shape[0])
    for i in range(label_mat.shape[0]):
        (values, counts) = np.unique(label_mat[i], return_counts=True)
        preds[i] = values[np.argmax(counts)]

    # produce acc
    label_T = np.asarray(dataset.labels_test)
    acc = (preds == label_T).mean() * 100

    result.acc_list += [acc]
    result.iter_list += [it]
    result.save_model = False
    if acc > result.best_acc:
        result.best_acc = acc
        result.best_iter = it
        result.save_model = True
    print("{}nn Classifier: ".format(opt.Knn))
    print("Test Accuracy is {:.4}%".format(acc))
    print("Best Test Acc is {:.4}%".format(result.best_acc))

""" Generalized ZSL"""
def eval_fakefeat_GZSL(it, netG, dataset, param, result):
    gen_feat = np.zeros([0, param.X_dim])
    for i in range(dataset.train_cls_num):
        text_feat = np.tile(dataset.train_text_feature[i].astype('float32'), (opt.nSample, 1))
        text_feat = Variable(torch.from_numpy(text_feat)).cuda()
        z = Variable(torch.randn(opt.nSample, param.z_dim)).cuda()
        G_sample = netG(z, text_feat)
        gen_feat = np.vstack((gen_feat, G_sample.data.cpu().numpy()))

    for i in range(dataset.test_cls_num):
        text_feat = np.tile(dataset.test_text_feature[i].astype('float32'), (opt.nSample, 1))
        text_feat = Variable(torch.from_numpy(text_feat)).cuda()
        z = Variable(torch.randn(opt.nSample, param.z_dim)).cuda()
        G_sample = netG(z, text_feat)
        gen_feat = np.vstack((gen_feat, G_sample.data.cpu().numpy()))

    visual_pivots = [gen_feat[i*opt.nSample:(i+1)*opt.nSample].mean(0) \
                     for i in range(dataset.train_cls_num + dataset.test_cls_num)]
    visual_pivots = np.vstack(visual_pivots)

    """collect points for gzsl curve"""

    train_acc = None
    train_auc = None
    acc_S_T_list, acc_U_T_list = list(), list()
    seen_sim = cosine_similarity(dataset.pfc_feat_data_train, visual_pivots)
    unseen_sim = cosine_similarity(dataset.pfc_feat_data_test, visual_pivots)
    for GZSL_lambda in np.arange(-2, 2, 0.01):
        tmp_seen_sim = copy.deepcopy(seen_sim)
        tmp_seen_sim[:, dataset.train_cls_num:] += GZSL_lambda
        pred_lbl = np.argmax(tmp_seen_sim, axis=1)
        acc_S_T_list.append((pred_lbl == np.asarray(dataset.labels_train)).mean())

        tmp_unseen_sim = copy.deepcopy(unseen_sim)
        tmp_unseen_sim[:, dataset.train_cls_num:] += GZSL_lambda
        pred_lbl = np.argmax(tmp_unseen_sim, axis=1)
        acc_U_T_list.append((pred_lbl == (np.asarray(dataset.labels_test)+dataset.train_cls_num)).mean())

    auc_score = integrate.trapz(y=acc_S_T_list, x=acc_U_T_list)

    result.acc_list += [auc_score]
    result.iter_list += [it]
    result.save_model = False
    if auc_score > result.best_acc:
        result.best_acc = auc_score
        result.best_iter = it
        result.save_model = True

    if np.mean(acc_S_T_list) > result.best_train_acc:
        result.best_train_acc = np.mean(acc_S_T_list)
        result.save_model = True
        pass
    print("AUC Score is {:.4}".format(auc_score))
    print("Train accuracy is {:.04}".format(np.mean(acc_S_T_list)))
    
class Result(object):
    def __init__(self):
        self.best_acc = 0.0
        self.best_iter = 0.0
        self.acc_list = []
        self.iter_list = []
        self.save_model = False
        self.best_train_acc = 0.0

def weights_init(m):
    classname = m.__class__.__name__
    if 'Linear' in classname:
        init.xavier_normal(m.weight.data)
        init.constant(m.bias, 0.0)


def reset_grad(nets):
    for net in nets:
        net.zero_grad()

def label2mat(labels, y_dim):
    c = np.zeros([labels.shape[0], y_dim])
    for idx, d in enumerate(labels):
        c[idx, d] = 1
    return c


def calc_gradient_penalty(netD, real_data, fake_data):
    alpha = torch.rand(opt.batchsize, 1)
    alpha = alpha.expand(real_data.size())
    alpha = alpha.cuda()

    interpolates = alpha * real_data + ((1 - alpha) * fake_data)
    interpolates = interpolates.cuda()
    interpolates = autograd.Variable(interpolates, requires_grad=True)

    disc_interpolates, _ = netD(interpolates)

    gradients = autograd.grad(outputs=disc_interpolates, inputs=interpolates,
                              grad_outputs=torch.ones(disc_interpolates.size()).cuda(),
                              create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * opt.GP_LAMBDA
    return gradient_penalty


if __name__ == "__main__":
    train()