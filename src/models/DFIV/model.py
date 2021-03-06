from typing import List, Optional
import torch
from torch import nn
import numpy as np
import logging

from src.utils.pytorch_linear_reg_utils import fit_linear, linear_reg_pred, outer_prod, add_const_col
from src.data.data_class import TrainDataSet, TestDataSet, TrainDataSetTorch, TestDataSetTorch

logger = logging.getLogger()


class DFIVModel:
    stage1_weight: torch.Tensor
    stage2_weight: torch.Tensor

    def __init__(self,
                 treatment_net: nn.Module,
                 instrumental_net: nn.Module,
                 covariate_net: Optional[nn.Module],
                 add_stage1_intercept: bool,
                 add_stage2_intercept: bool
                 ):
        self.treatment_net = treatment_net
        self.instrumental_net = instrumental_net
        self.covariate_net = covariate_net
        self.add_stage1_intercept = add_stage1_intercept
        self.add_stage2_intercept = add_stage2_intercept


    @staticmethod
    def augment_stage1_feature(instrumental_feature: torch.Tensor,
                               add_stage1_intercept: bool):

        feature = instrumental_feature
        if add_stage1_intercept:
            feature = add_const_col(feature)
        return feature

    @staticmethod
    def augment_stage2_feature(predicted_treatment_feature: torch.Tensor,
                               covariate_feature: Optional[torch.Tensor],
                               add_stage2_intercept: bool):
        feature = predicted_treatment_feature
        if add_stage2_intercept:
            feature = add_const_col(feature)

        if covariate_feature is not None:
            feature_tmp = covariate_feature
            if add_stage2_intercept:
                feature_tmp = add_const_col(feature_tmp)
            feature = outer_prod(feature, feature_tmp)
            feature = torch.flatten(feature, start_dim=1)

        return feature

    @staticmethod
    def fit_2sls(treatment_1st_feature: torch.Tensor,
                 instrumental_1st_feature: torch.Tensor,
                 instrumental_2nd_feature: torch.Tensor,
                 covariate_2nd_feature: Optional[torch.Tensor],
                 outcome_2nd_t: torch.Tensor,
                 lam1: float, lam2: float,
                 add_stage1_intercept: bool,
                 add_stage2_intercept: bool,
                 ):

        # stage1
        feature = DFIVModel.augment_stage1_feature(instrumental_1st_feature, add_stage1_intercept)
        stage1_weight = fit_linear(treatment_1st_feature, feature, lam1)

        # predicting for stage 2
        feature = DFIVModel.augment_stage1_feature(instrumental_2nd_feature,
                                                   add_stage1_intercept)
        predicted_treatment_feature = linear_reg_pred(feature, stage1_weight)

        # stage2
        feature = DFIVModel.augment_stage2_feature(predicted_treatment_feature,
                                                   covariate_2nd_feature,
                                                   add_stage2_intercept)

        stage2_weight = fit_linear(outcome_2nd_t, feature, lam2)
        pred = linear_reg_pred(feature, stage2_weight)
        stage2_loss = torch.norm((outcome_2nd_t - pred)) ** 2 + lam2 * torch.norm(stage2_weight) ** 2

        return dict(stage1_weight=stage1_weight,
                    predicted_treatment_feature=predicted_treatment_feature,
                    stage2_weight=stage2_weight,
                    stage2_loss=stage2_loss)

    def fit_t(self,
              train_1st_data_t: TrainDataSetTorch,
              train_2nd_data_t: TrainDataSetTorch,
              lam1: float, lam2: float):

        treatment_1st_feature = self.treatment_net(train_1st_data_t.treatment)
        instrumental_1st_feature = self.instrumental_net(train_1st_data_t.instrumental)
        instrumental_2nd_feature = self.instrumental_net(train_2nd_data_t.instrumental)
        outcome_2nd_t = train_2nd_data_t.outcome
        covariate_2nd_feature = None
        if self.covariate_net is not None:
            covariate_2nd_feature = self.covariate_net(train_2nd_data_t.covariate)

        res = DFIVModel.fit_2sls(treatment_1st_feature,
                                 instrumental_1st_feature,
                                 instrumental_2nd_feature,
                                 covariate_2nd_feature,
                                 outcome_2nd_t,
                                 lam1, lam2,
                                 self.add_stage1_intercept,
                                 self.add_stage2_intercept)

        self.stage1_weight = res["stage1_weight"]
        self.stage2_weight = res["stage2_weight"]

    def fit(self, train_1st_data: TrainDataSet, train_2nd_data: TrainDataSet, lam1: float, lam2: float):
        train_1st_data_t = TrainDataSetTorch.from_numpy(train_1st_data)
        train_2nd_data_t = TrainDataSetTorch.from_numpy(train_2nd_data)
        self.fit_t(train_1st_data_t, train_2nd_data_t, lam1, lam2)

    def predict_t(self, treatment: torch.Tensor, covariate: Optional[torch.Tensor]):
        treatment_feature = self.treatment_net(treatment)
        covariate_feature = None
        if self.covariate_net:
            covariate_feature = self.covariate_net(covariate)

        feature = DFIVModel.augment_stage2_feature(treatment_feature,
                                                   covariate_feature,
                                                   self.add_stage2_intercept)
        return linear_reg_pred(feature, self.stage2_weight)

    def predict(self, treatment: np.ndarray, covariate: Optional[np.ndarray]):
        treatment_t = torch.tensor(treatment, dtype=torch.float32)
        covariate_t = None
        if covariate is not None:
            covariate_t = torch.tensor(covariate, dtype=torch.float32)
        return self.predict_t(treatment_t, covariate_t).data.numpy()

    def evaluate_t(self, test_data: TestDataSetTorch):
        target = test_data.structural
        with torch.no_grad():
            pred = self.predict_t(test_data.treatment, test_data.covariate)
        return (torch.norm((target - pred)) ** 2) / target.size()[0]

    def evaluate(self, test_data: TestDataSet):
        return self.evaluate_t(TestDataSetTorch.from_numpy(test_data)).data.item()
