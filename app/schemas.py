"""HTTP 请求体的 Pydantic 模型与按事件类型的必填字段校验。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal["transfer", "receive", "seal_check", "storage"]
CorrectionAction = Literal["void", "replace"]

# 各事件类型的必填字段（事件上报与纠错替换共用）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "transfer": ["transfer_no", "from_custodian", "to_custodian"],
    "receive": ["transfer_no", "receiver", "seal_no"],
    "seal_check": ["checked_seal_no"],
    "storage": [],
}


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号，业务唯一键")
    collected_at: datetime = Field(..., description="采集时间 ISO8601")
    collector: str = Field(..., min_length=1, description="采集人，即初始保管人")
    seal_no: str = Field(..., min_length=1, description="封签号")


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，同号重试幂等")
    event_type: EventType
    sample_code: str = Field(..., min_length=1, description="样本编号")
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "EventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class NewEventIn(BaseModel):
    """纠错替换时提交的完整新事件内容（不含 sample_code —— 纠错不得跨样本）。

    字段集与校验规则与 EventIn 一致；新事件号必须全局唯一且未被占用。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="新事件号，必须未被占用（全局唯一）")
    event_type: EventType
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "NewEventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class CorrectionIn(BaseModel):
    """核定前事件纠错：作废（void）或替换（replace）。

    * 仅核定入库前可纠错，纠错不得跨样本；
    * 同一纠错号同内容重试幂等（200），异内容冲突（409）；
    * replace 必须给出完整新事件内容与未占用的新事件号；void 不得携带新事件。
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: str = Field(..., min_length=1, description="全局唯一纠错号，同号重试幂等")
    sample_code: str = Field(..., min_length=1, description="样本编号（纠错不得跨样本）")
    target_event_id: str = Field(..., min_length=1, description="被作废/替换的当前有效事件号")
    action: CorrectionAction = Field(..., description="void=作废，replace=替换")
    operator: str = Field(..., min_length=1, description="纠错操作人")
    reason: str = Field(..., min_length=1, description="纠错原因")
    new_event: NewEventIn | None = Field(None, description="替换时的完整新事件内容；作废时必须为空")

    @model_validator(mode="after")
    def _check_action_payload(self) -> "CorrectionIn":
        if self.action == "replace" and self.new_event is None:
            raise ValueError("action=replace 必须提供 new_event（完整新事件内容与未占用的新事件号）")
        if self.action == "void" and self.new_event is not None:
            raise ValueError("action=void 作废不得携带 new_event")
        return self


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(..., min_length=1, description="核定操作人")
    note: str | None = Field(None, description="核定备注")


class BatchTransferItemIn(BaseModel):
    """同一车次移交批次内的一份样本移交。

    事件类型固定为 transfer；移交时间、交出人、接收人取自批次公共字段，
    每份仅需提交样本编号、移交单号与该份移交的事件号。
    """

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="本份样本编号（批内不得重复）")
    transfer_no: str = Field(..., min_length=1, description="本份移交单号")
    event_id: str = Field(..., min_length=1, description="本份移交事件号（全局唯一，批内不得重复）")


class BatchTransferIn(BaseModel):
    """车次移交批次：多份样本的 transfer 事件作为既有事件一次提交。

    * 批次号全局唯一；同批次号同内容重试幂等（即使样本已核定），异内容 409；
    * 批内样本编号 / 事件号重复、样本不存在或已核定、任一事件号已占用 → 整批失败不留痕；
    * 全部份内事件均为 event_type=transfer，移交时间/交出人/接收人整批统一。
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1, description="全局唯一批次号，同号重试幂等")
    transfer_time: datetime = Field(..., description="本批统一移交时间 ISO8601")
    from_custodian: str = Field(..., min_length=1, description="交出人（整批一致）")
    to_custodian: str = Field(..., min_length=1, description="接收人（整批一致）")
    items: list[BatchTransferItemIn] = Field(..., min_length=1, description="批内各份样本移交（至少一份）")
    note: str | None = Field(None, description="批次备注（不写入单事件指纹，仅随批次留档）")
