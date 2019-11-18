import numpy as np

class UnsupervisedData():

    def __init__(self, test_text_feature, labels_test, pfc_feat_data_test, train_cls_num):
        self.labels = np.array(labels_test)
        self.image_feature = pfc_feat_data_test
        self.text_feature = np.zeros((pfc_feat_data_test.shape[0], test_text_feature.shape[1]))
        self.label_index = train_cls_num
        self.unsupervised_label_mapping = {}
        for i, label in enumerate(self.labels):
            self.text_feature[i] = test_text_feature[label]
