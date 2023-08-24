# -*- coding:utf-8 -*-
"""
Author:
    zanshuxun, zanshuxun@aliyun.com

Reference:
    [1] Jiaqi Ma, Zhe Zhao, Xinyang Yi, et al. Modeling Task Relationships in Multi-task Learning with Multi-gate Mixture-of-Experts[C] (https://dl.acm.org/doi/10.1145/3219819.3220007)
"""
import torch
import torch.nn as nn
from .satrans import SelfAttention_Layer,TargetAttention_Layer,Attention_Layer
from .mtl_basemodel import BaseModel
from deepctr_torch.inputs import combined_dnn_input
from deepctr_torch.layers import DNN, PredictionLayer
import torch.nn.functional as F

def concat_fun(inputs, axis=-1):
    if len(inputs) == 1:
        return inputs[0]
    else:
        return torch.cat(inputs, dim=axis)

class MMOE_MT_ATT(BaseModel):
    """Instantiates the Multi-gate Mixture-of-Experts architecture.

    :param dnn_feature_columns: An iterable containing all the features used by deep part of the model.
    :param num_experts: integer, number of experts.
    :param expert_dnn_hidden_units: list, list of positive integer or empty list, the layer number and units in each layer of expert DNN.
    :param gate_dnn_hidden_units: list, list of positive integer or empty list, the layer number and units in each layer of gate DNN.
    :param tower_dnn_hidden_units: list, list of positive integer or empty list, the layer number and units in each layer of task-specific DNN.
    :param l2_reg_linear: float, L2 regularizer strength applied to linear part.
    :param l2_reg_embedding: float, L2 regularizer strength applied to embedding vector.
    :param l2_reg_dnn: float, L2 regularizer strength applied to DNN.
    :param init_std: float, to use as the initialize std of embedding vector.
    :param seed: integer, to use as random seed.
    :param dnn_dropout: float in [0,1), the probability we will drop out a given DNN coordinate.
    :param dnn_activation: Activation function to use in DNN.
    :param dnn_use_bn: bool, Whether use BatchNormalization before activation or not in DNN.
    :param task_types: list of str, indicating the loss of each tasks, ``"binary"`` for  binary logloss, ``"regression"`` for regression loss. e.g. ['binary', 'regression'].
    :param task_names: list of str, indicating the predict target of each tasks.
    :param device: str, ``"cpu"`` or ``"cuda:0"``.
    :param gpus: list of int or torch.device for multiple gpus. If None, run on `device`. `gpus[0]` should be the same gpu with `device`.

    :return: A PyTorch model instance.
    """

    def __init__(self, dnn_feature_columns, num_domains,num_experts=3, expert_dnn_hidden_units=(256, 128),
                 gate_dnn_hidden_units=(64,), tower_dnn_hidden_units=(64,), l2_reg_linear=0.00001,
                 l2_reg_embedding=0.00001, l2_reg_dnn=0,
                 init_std=0.0001, seed=1024, dnn_dropout=0, dnn_activation='relu', dnn_use_bn=False,
                 task_types=('binary', 'binary'), task_names=('ctr', 'ctcvr'), device='cpu', gpus=None,domain_column=None,flag=None,domain_id_as_feature=False):
        super(MMOE_MT_ATT, self).__init__(linear_feature_columns=[], dnn_feature_columns=dnn_feature_columns,
                                   l2_reg_linear=l2_reg_linear, l2_reg_embedding=l2_reg_embedding, init_std=init_std,
                                   seed=seed, device=device, gpus=gpus)
        self.num_tasks = len(task_names)
        self.domain_column=domain_column
        self.flag=flag

        if self.num_tasks <= 1:
            raise ValueError("num_tasks must be greater than 1")
        if num_experts <= 1:
            raise ValueError("num_experts must be greater than 1")
        if len(dnn_feature_columns) == 0:
            raise ValueError("dnn_feature_columns is null!")
        if len(task_types) != self.num_tasks:
            raise ValueError("num_tasks must be equal to the length of task_types")

        for task_type in task_types:
            if task_type not in ['binary', 'regression']:
                raise ValueError("task must be binary or regression, {} is illegal".format(task_type))

        self.num_experts = num_experts
        self.task_names = task_names
        self.input_dim = self.compute_input_dim(dnn_feature_columns)
        self.expert_dnn_hidden_units = expert_dnn_hidden_units
        self.gate_dnn_hidden_units = gate_dnn_hidden_units
        self.tower_dnn_hidden_units = tower_dnn_hidden_units
        if 'usetrans' in self.flag:
            self.int_layers = nn.ModuleList(
                [SelfAttention_Layer(self.embedding_dim, 4, True, device=device) for _ in range(3)])


        # expert dnn
        self.expert_dnn = nn.ModuleList([DNN(self.input_dim, expert_dnn_hidden_units, activation=dnn_activation,
                                             l2_reg=l2_reg_dnn, dropout_rate=dnn_dropout, use_bn=dnn_use_bn,
                                             init_std=init_std, device=device) for _ in range(self.num_experts)])

        # gate dnn
        if len(gate_dnn_hidden_units) > 0:
            self.gate_dnn = nn.ModuleList([DNN(self.input_dim, gate_dnn_hidden_units, activation=dnn_activation,
                                               l2_reg=l2_reg_dnn, dropout_rate=dnn_dropout, use_bn=dnn_use_bn,
                                               init_std=init_std, device=device) for _ in range(self.num_tasks)])
            self.add_regularization_weight(
                filter(lambda x: 'weight' in x[0] and 'bn' not in x[0], self.gate_dnn.named_parameters()),
                l2=l2_reg_dnn)
        self.gate_dnn_final_layer = nn.ModuleList(
            [nn.Linear(gate_dnn_hidden_units[-1] if len(gate_dnn_hidden_units) > 0 else self.input_dim,
                       self.num_experts, bias=False) for _ in range(self.num_tasks)])

        # tower dnn (task-specific)
        if len(tower_dnn_hidden_units) > 0:
            self.tower_dnn = nn.ModuleList(
                [DNN(expert_dnn_hidden_units[-1], tower_dnn_hidden_units, activation=dnn_activation,
                     l2_reg=l2_reg_dnn, dropout_rate=dnn_dropout, use_bn=dnn_use_bn,
                     init_std=init_std, device=device) for _ in range(self.num_tasks)])
            self.add_regularization_weight(
                filter(lambda x: 'weight' in x[0] and 'bn' not in x[0], self.tower_dnn.named_parameters()),
                l2=l2_reg_dnn)
        self.tower_dnn_final_layer = nn.ModuleList([nn.Linear(
            tower_dnn_hidden_units[-1] if len(tower_dnn_hidden_units) > 0 else expert_dnn_hidden_units[-1], 1,
            bias=False)
                                                    for _ in range(self.num_tasks)])

        self.out = nn.ModuleList([PredictionLayer(task) for task in task_types])

        regularization_modules = [self.expert_dnn, self.gate_dnn_final_layer, self.tower_dnn_final_layer]
        for module in regularization_modules:
            self.add_regularization_weight(
                filter(lambda x: 'weight' in x[0] and 'bn' not in x[0], module.named_parameters()), l2=l2_reg_dnn)
        self.to(device)
        #domain
        if domain_id_as_feature:
            field_num = len(self.embedding_dict)
        else:
            dnn_feature_columns = self.filter_feature_columns(dnn_feature_columns, domain_column)
            field_num = len(self.embedding_dict) - 1
        self.domain_column = domain_column
        embedding_size = self.embedding_size
        # print('num_domains',num_domains)
        self.domain_embeddings = nn.Embedding(num_domains+1, embedding_size)
        self.lhuc = nn.ModuleList([nn.ModuleList([
                        nn.Linear(embedding_size, 128, bias=False),
                        nn.ReLU(),
                        nn.Linear(128, 64, bias=False),
                        nn.Sigmoid()
                    ]) for _ in range(self.num_tasks)])
        self.target_attention = nn.ModuleList([Attention_Layer(self.embedding_size, 4, True, device=device) for _ in range(self.num_tasks)])
        self.to(device)
    def forward(self, X):
        sparse_embedding_list, dense_value_list = self.input_from_feature_columns(X, self.dnn_feature_columns,
                                                                                  self.embedding_dict)
        #dnn_input = combined_dnn_input(sparse_embedding_list, dense_value_list)


        if 'usetrans' in self.flag:
            att_input = concat_fun(sparse_embedding_list, axis=1)
            for layer in self.int_layers:
                att_input = layer(att_input)
            att_output = torch.flatten(att_input, start_dim=1)
            if len(dense_value_list)>0:
                dense_input = concat_fun(dense_value_list, axis=1)
                dnn_input = concat_fun([att_output, dense_input])
            else:
                dnn_input=att_output

        else:
            dnn_input = combined_dnn_input(sparse_embedding_list, dense_value_list)


        # expert dnn
        expert_outs = []
        for i in range(self.num_experts):
            expert_out = self.expert_dnn[i](dnn_input)
            expert_outs.append(expert_out)
        expert_outs = torch.stack(expert_outs, 1)  # (bs, num_experts, dim)

        # gate dnn
        mmoe_outs = []
        for i in range(self.num_tasks):
            if len(self.gate_dnn_hidden_units) > 0:
                gate_dnn_out = self.gate_dnn[i](dnn_input)
                gate_dnn_out = self.gate_dnn_final_layer[i](gate_dnn_out)
            else:
                gate_dnn_out = self.gate_dnn_final_layer[i](dnn_input)
            gate_mul_expert = torch.matmul(gate_dnn_out.softmax(1).unsqueeze(1), expert_outs)  # (bs, 1, dim)
            mmoe_outs.append(gate_mul_expert.squeeze())
        # 将以下代码适配到pytorch
        # att_emb_size = 128
        # q = layers.fully_connected(input, att_emb_size, activation_fn=tf.nn.relu)
        # k = layers.fully_connected(domain, att_emb_size, activation_fn=tf.nn.relu)
        # v = layers.fully_connected(domain, att_emb_size, activation_fn=tf.nn.relu)
        # att = tf.matmul(q, k, transpose_b=True)
        # att = tf.nn.softmax(att)
        # att = tf.matmul(att, v)
        

        # domain

        domain_ids = X[:, self.feature_index[self.domain_column][0]].long().to(self.device)
        domain_emb = self.domain_embeddings(domain_ids)

        # print("domain_emb",domain_emb.shape)

        # tower dnn (task-specific)
        task_outs = []
        for i in range(self.num_tasks):
            if len(self.tower_dnn_hidden_units) > 0:
                tower_dnn_out = self.tower_dnn[i](mmoe_outs[i])
                att = self.target_attention[i](tower_dnn_out, domain_emb)
                # print('att',att)
                for layer in self.lhuc[i]:
                    att = layer(att)
                # print(f"task:{i},tower_dnn_out:{tower_dnn_out.shape}")
                tower_dnn_out = tower_dnn_out*att
                tower_dnn_logit = self.tower_dnn_final_layer[i](tower_dnn_out)
            else:
                tower_dnn_logit = self.tower_dnn_final_layer[i](mmoe_outs[i])
            output = self.out[i](tower_dnn_logit)
            task_outs.append(output)
        task_outs = torch.cat(task_outs, -1)
        # print("task_outs",task_outs.shape)
        return task_outs
    def filter_feature_columns(self, feature_columns, filtered_col_names):
            return [feat for feat in feature_columns if feat.name not in filtered_col_names]
# $
# python main.py --data_name alicpp --model_name MMOE_MT_ATT --seed 1021 --embedding_dim 32 --learning_rate 0.005 --domain_att_layer_num 3 --att_head_num 4 --meta_mode QK --domain_col 301 --flag sota
# python main.py --data_name alicpp --model_name MMOE --seed 1021 --embedding_dim 32 --learning_rate 0.005 --domain_att_layer_num 3 --att_head_num 4 --meta_mode QK --domain_col 301 --flag sota

# python main_gen.py --data_name alicpp --model_name MMOE_MT_ATT --seed 1021 --embedding_dim 32 --learning_rate 0.005 --domain_att_layer_num 3 --att_head_num 4 --meta_mode QK --domain_col 301 --flag sota

# Train on 42299905 samples, validate on 0 samples, 5164 steps per epoch
# base
# 5164it [32:51,  2.62it/s]
# test AUC 0.6179
# Domain 1 test AUC 0.6214
# Domain 2 test AUC 0.6161
# Domain 3 test AUC 0.5942
# 08-21-13-35

# lhuc
# test AUC 0.6187
# Domain 1 test AUC 0.622
# Domain 2 test AUC 0.6168
# Domain 3 test AUC 0.5965

# lhuc+att
# test AUC 0.615
# Domain 1 test AUC 0.6191
# Domain 2 test AUC 0.6141
# Domain 3 test AUC 0.595

# lhuc每个任务不同
# test AUC 0.6152
# Domain 1 test AUC 0.6178
# Domain 2 test AUC 0.6134
# Domain 3 test AUC 0.5944

# norm 不用 dropout
# test AUC 0.6141
# Domain 1 test AUC 0.6184
# Domain 2 test AUC 0.6114
# Domain 3 test AUC 0.5961

# 没有残差
# test AUC 0.6151
# Domain 1 test AUC 0.6183
# Domain 2 test AUC 0.613
# Domain 3 test AUC 0.5929

# 修复att sclae错误
# test AUC 0.6152
# Domain 1 test AUC 0.6189
# Domain 2 test AUC 0.6128
# Domain 3 test AUC 0.5941
# 08-24-15-21
# att中间层加宽

# export url='https://fastly.jsdelivr.net/gh/juewuy/ShellClash@master' && sudo wget -q --no-check-certificate -O /tmp/install.sh $url/install.sh  && sudo bash /tmp/install.sh && source /etc/profile &> /dev/null

# wget -N --no-check-certificate https://github.com/Dreamacro/clash/releases/download/v1.18.0/clash-linux-amd64-v1.18.0.gz