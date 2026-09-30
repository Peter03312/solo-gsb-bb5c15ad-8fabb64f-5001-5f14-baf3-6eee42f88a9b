"""端到端自测：同一车次多份样本的移交批次（transfer batch）。

覆盖：
  * 整批一次提交为既有 transfer 事件（逐份逐字段与单事件接口一致）；
  * 整批失败不留痕（批内重复 / 样本不存在 / 已核定 / 事件号占用）；
  * 同批次号同内容幂等（含乱序、核定后），异内容冲突；
  * 批次查询返回逐份当前核验结论与原事件有效性，作废/替换只改有效链、归属可追溯；
  * 并发批次与核定按已提交顺序判定；并发批次竞争事件号恰一批成功且败者全量回滚；
  * 既有单事件接口 / 纠错 / 核定行为不变，批次事件与单事件互认指纹。

运行：python -m tests.test_batches
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_batches.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="张三", seal=None, when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when,
        "collector": collector, "seal_no": seal or f"SEAL-{code}",
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def batch(body):
    return client.post(f"{API}/transfer-batches", json=body)


def get_batch(bid):
    return client.get(f"{API}/transfer-batches/{bid}")


def batch_body(bid, codes, eids=None, transfer_nos=None, **over):
    """便捷构造 2 份以上的批次（默认每码一份）。"""
    eids = eids or [f"{bid}-E{i}" for i, _ in enumerate(codes)]
    transfer_nos = transfer_nos or [f"{bid}-TR{i}" for i, _ in enumerate(codes)]
    body = {
        "batch_id": bid,
        "transfer_time": "2026-09-30T02:00:00Z",
        "from_custodian": "张三",
        "to_custodian": "李四",
        "items": [
            {"sample_code": c, "transfer_no": t, "event_id": e}
            for c, t, e in zip(codes, transfer_nos, eids)
        ],
    }
    body.update(over)
    return body


def item_of(payload: dict, code: str) -> dict:
    return next(it for it in payload["items"] if it["sample_code"] == code)


def eval_of(bresp, code):
    return item_of(bresp.json(), code)


# ---------------------------------------------------------------- 1. 整批一次提交
print("1) 整批提交：多份样本各生成一条既有 transfer 事件")
reg("B-S1"); reg("B-S2")
r = batch(batch_body("BATCH-1", ["B-S1", "B-S2"]))
check("批次提交 201", r.status_code == 201, r.text)
b = r.json()
check("total=2 且公共字段回显",
      b["total"] == 2 and b["batch_id"] == "BATCH-1"
      and b["transfer_time"] == "2026-09-30T02:00:00Z"
      and b["from_custodian"] == "张三" and b["to_custodian"] == "李四"
      and b["note"] is None, str(b))
check("逐份结论 pending（已移交未签收未入库）",
      all(it["evaluation"]["status"] == "pending" for it in b["items"]), str(b["items"]))
check("逐份原事件当前有效",
      all(it["original_event_effective"] is True for it in b["items"]), str(b["items"]))
check("逐份归属字段齐全（编号/移交单/事件号/顺序）",
      [(it["position"], it["sample_code"], it["transfer_no"], it["event_id"]) for it in b["items"]]
      == [(0, "B-S1", "BATCH-1-TR0", "BATCH-1-E0"),
          (1, "B-S2", "BATCH-1-TR1", "BATCH-1-E1")], str(b["items"]))

# 事件确实以既有 transfer 形态落库（时间线/保管人语义与单事件一致）
c1 = chain("B-S1")
t1 = [e for e in c1["timeline"] if e["event_id"] == "BATCH-1-E0"]
check("样本链时间线含批次移交事件且为 transfer",
      len(t1) == 1 and t1[0]["event_type"] == "transfer"
      and t1[0]["payload"] == {"transfer_no": "BATCH-1-TR0",
                               "from_custodian": "张三", "to_custodian": "李四"},
      str(t1))
check("批次事件时间线带 batch_id 归属", t1[0]["batch_id"] == "BATCH-1", str(t1[0]))
check("移交后保管人仍为交出人（签收前不变更）", c1["current_custodian"] == "张三")
check("在途移交单正确", c1["pending_transfer"]["transfer_no"] == "BATCH-1-TR0",
      str(c1["pending_transfer"]))

# 批次写入的事件可正常被签收关闭并入库（证明“作为既有 transfer 事件”参与核验）
ev({"event_id": "B-S1-R", "event_type": "receive", "sample_code": "B-S1",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "BATCH-1-TR0",
    "receiver": "李四", "seal_no": "SEAL-B-S1"})
ev({"event_id": "B-S1-S", "event_type": "storage", "sample_code": "B-S1",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
check("批次移交被正确签收后可入库 valid", chain("B-S1")["status"] == "valid",
      str(chain("B-S1")["reasons"]))

# ---------------------------------------------------------------- 2. 整批失败不留痕
print("2) 任一校验失败：整批拒绝，不留下事件或批次")
# 2.1 批内样本编号重复
reg("B-DUP")
r = batch(batch_body("BATCH-DUP", ["B-DUP", "B-DUP"],
                     eids=["DUP-E0", "DUP-E1"], transfer_nos=["DUP-T0", "DUP-T1"]))
check("批内样本重复 422 batch_duplicate_items",
      r.status_code == 422 and r.json()["error"]["code"] == "batch_duplicate_items"
      and r.json()["error"]["details"]["duplicate_sample_codes"] == ["B-DUP"], r.text)

# 2.2 批内事件号重复（不同样本、同事件号）
reg("B-DUP2")
r = batch(batch_body("BATCH-DUPE", ["B-DUP", "B-DUP2"],
                     eids=["DUP-E", "DUP-E"], transfer_nos=["DT0", "DT1"]))
check("批内事件号重复 422 batch_duplicate_items",
      r.status_code == 422 and r.json()["error"]["code"] == "batch_duplicate_items"
      and r.json()["error"]["details"]["duplicate_event_ids"] == ["DUP-E"], r.text)

# 2.3 样本不存在
r = batch(batch_body("BATCH-MISS", ["B-DUP", "B-NOPE"],
                     eids=["M-E0", "M-E1"], transfer_nos=["M-T0", "M-T1"]))
check("批内样本不存在 404 sample_not_found 并列出缺失样本",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found"
      and r.json()["error"]["details"]["missing_sample_codes"] == ["B-NOPE"], r.text)

# 2.4 事件号已被既有事件（单事件/其它批次/历史留痕）占用
r = batch(batch_body("BATCH-OCC", ["B-DUP"], eids=["BATCH-1-E1"], transfer_nos=["OCC-T"]))
check("事件号已占用 409 event_id_occupied",
      r.status_code == 409 and r.json()["error"]["code"] == "event_id_occupied"
      and r.json()["error"]["details"]["occupied_event_ids"] == ["BATCH-1-E1"], r.text)

# 2.5 已核定样本（同批次其它样本未核定也整批拒绝）
r = client.post(f"{API}/samples/B-S1/approve", json={"approver": "主管"})
check("前置：B-S1 已核定", r.status_code == 200, r.text)
reg("B-LOCKED-FRIEND")
r = batch(batch_body("BATCH-LOCK", ["B-S1", "B-LOCKED-FRIEND"],
                     eids=["LK-E0", "LK-E1"], transfer_nos=["LK-T0", "LK-T1"]))
check("含已核定样本 409 history_locked 整批拒绝",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked"
      and r.json()["error"]["details"]["locked_sample_codes"] == ["B-S1"], r.text)

# 2.6 失败后无任何批次头 / 归属行 / 事件残留
import sqlite3  # noqa: E402

from app import config  # noqa: E402

con = sqlite3.connect(str(config.DB_PATH))
failed_bids = ("BATCH-DUP", "BATCH-DUPE", "BATCH-MISS", "BATCH-OCC", "BATCH-LOCK")
check("失败批次头不留存",
      con.execute(
          f"SELECT COUNT(*) FROM transfer_batch WHERE batch_id IN ({','.join('?' * len(failed_bids))})",
          failed_bids).fetchone()[0] == 0)
check("失败批次归属行不留存",
      con.execute(
          "SELECT COUNT(*) FROM transfer_batch_item i JOIN transfer_batch b ON i.batch_pk = b.id"
          f" WHERE b.batch_id IN ({','.join('?' * len(failed_bids))})",
          failed_bids).fetchone()[0] == 0)
check("失败批次事件不留存",
      con.execute(
          "SELECT COUNT(*) FROM custody_event WHERE event_id IN "
          "('DUP-E0','DUP-E1','DUP-E','M-E0','M-E1','OCC-E','LK-E0','LK-E1')").fetchone()[0] == 0)
con.close()
check("失败批次查询 404 batch_not_found",
      get_batch("BATCH-MISS").status_code == 404
      and get_batch("BATCH-MISS").json()["error"]["code"] == "batch_not_found")

# 被整批拒绝占用的批次号/事件号可重新使用
r = batch(batch_body("BATCH-MISS", ["B-DUP"], eids=["M-E0"], transfer_nos=["M-T0"]))
check("此前失败的批次号/事件号可重新提交 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 3. 幂等与冲突
print("3) 同批次号：同内容幂等（乱序/核定后）、异内容冲突")
same = batch(batch_body("BATCH-1", ["B-S1", "B-S2"]))
check("同内容重试 200", same.status_code == 200 and same.json()["batch_id"] == "BATCH-1", same.text)
check("幂等响应 total 仍为 2、逐份结果实时返回",
      same.json()["total"] == 2
      and eval_of(same, "B-S1")["evaluation"]["status"] == "valid", str(same.json()["items"]))

# 提交顺序不影响幂等（批内逐份集合相同即同内容）
original_items = [
    {"sample_code": "B-S1", "transfer_no": "BATCH-1-TR0", "event_id": "BATCH-1-E0"},
    {"sample_code": "B-S2", "transfer_no": "BATCH-1-TR1", "event_id": "BATCH-1-E1"},
]
reordered_body = {**batch_body("BATCH-1", ["B-S1", "B-S2"]), "items": list(reversed(original_items))}
reordered = batch(reordered_body)
check("批内顺序打乱仍视为同内容 200", reordered.status_code == 200, reordered.text)
# position 仍按首次提交的原始顺序
check("原始提交顺序可追溯（position 不随重试顺序变化）",
      [(it["position"], it["sample_code"]) for it in reordered.json()["items"]]
      == [(0, "B-S1"), (1, "B-S2")], str(reordered.json()["items"]))

diff_to = batch(batch_body("BATCH-1", ["B-S1", "B-S2"], to_custodian="王五"))
check("接收人不同 409 batch_conflict",
      diff_to.status_code == 409 and diff_to.json()["error"]["code"] == "batch_conflict", diff_to.text)
r2 = batch({**batch_body("BATCH-1", ["B-S1", "B-S2"]), "transfer_time": "2026-09-30T02:30:00Z"})
check("移交时间不同 409 batch_conflict",
      r2.status_code == 409 and r2.json()["error"]["code"] == "batch_conflict", r2.text)
r2 = batch({**batch_body("BATCH-1", ["B-S1", "B-S2"]), "items": [
    {"sample_code": "B-S1", "transfer_no": "BATCH-1-TR0", "event_id": "BATCH-1-E0"},
    {"sample_code": "B-S2", "transfer_no": "BATCH-1-TR1", "event_id": "CHANGED-EID"}]})
check("份内事件号不同 409 batch_conflict",
      r2.status_code == 409 and r2.json()["error"]["code"] == "batch_conflict", r2.text)
reg("B-EXTRA")
r2 = batch({**batch_body("BATCH-1", ["B-S1", "B-S2"]), "items":
            batch_body("BATCH-1", ["B-S1", "B-S2", "B-EXTRA"],
                       eids=["BATCH-1-E0", "BATCH-1-E1", "BATCH-1-E2"],
                       transfer_nos=["BATCH-1-TR0", "BATCH-1-TR1", "BATCH-1-TR2"])["items"]})
check("份内条目数不同 409 batch_conflict",
      r2.status_code == 409 and r2.json()["error"]["code"] == "batch_conflict", r2.text)
r2 = batch({**batch_body("BATCH-1", ["B-S1", "B-S2"]), "note": "追加备注"})
check("批次备注不同也属异内容 409",
      r2.status_code == 409 and r2.json()["error"]["code"] == "batch_conflict", r2.text)
# 冲突/幂等失败均不改变既有批次归属
check("冲突尝试后批次仍为原 2 份",
      get_batch("BATCH-1").json()["total"] == 2
      and [it["event_id"] for it in get_batch("BATCH-1").json()["items"]]
      == ["BATCH-1-E0", "BATCH-1-E1"])

# B-S1 已核定：同批次号同内容重试仍幂等 200（核定后幂等不被冻结拦截）
post_lock = batch(batch_body("BATCH-1", ["B-S1", "B-S2"]))
check("样本核定后同批次同内容重试仍 200 幂等", post_lock.status_code == 200, post_lock.text)
check("幂等返回逐份当前结论：B-S1=valid、已核定标记",
      eval_of(post_lock, "B-S1")["evaluation"]["status"] == "valid"
      and eval_of(post_lock, "B-S1")["evaluation"]["approved_storage_at"] is not None,
      str(post_lock.json()["items"]))

# ---------------------------------------------------------------- 4. 与既有单事件接口互认
print("4) 批次事件与既有单事件接口互认指纹（200/409）")
same_ev = ev({"event_id": "BATCH-1-E1", "event_type": "transfer", "sample_code": "B-S2",
              "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "BATCH-1-TR1",
              "from_custodian": "张三", "to_custodian": "李四"})
check("单事件接口对批次事件同内容重试 200", same_ev.status_code == 200
      and same_ev.json()["idempotent"] is True, same_ev.text)
diff_ev = ev({"event_id": "BATCH-1-E1", "event_type": "transfer", "sample_code": "B-S2",
              "occurred_at": "2026-09-30T02:05:00Z", "transfer_no": "BATCH-1-TR1",
              "from_custodian": "张三", "to_custodian": "李四"})
check("单事件接口对批次事件异内容 409 event_conflict",
      diff_ev.status_code == 409 and diff_ev.json()["error"]["code"] == "event_conflict", diff_ev.text)
# 核定后的样本：单事件接口对批次事件同样保持 200/409 兼容
same_locked = ev({"event_id": "BATCH-1-E0", "event_type": "transfer", "sample_code": "B-S1",
                  "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "BATCH-1-TR0",
                  "from_custodian": "张三", "to_custodian": "李四"})
check("核定样本：批次事件同内容经单事件接口仍 200", same_locked.status_code == 200, same_locked.text)
new_locked = ev({"event_id": "BATCH-NEW-LK", "event_type": "transfer", "sample_code": "B-S1",
                 "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-X",
                 "from_custodian": "张三", "to_custodian": "李四"})
check("核定样本：新事件经单事件接口仍 409 history_locked（行为不变）",
      new_locked.status_code == 409 and new_locked.json()["error"]["code"] == "history_locked",
      new_locked.text)

# 反向：先由单事件接口写入的事件号，批次不得再占用
reg("B-PRE")
ev({"event_id": "PRE-E", "event_type": "transfer", "sample_code": "B-PRE",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "PRE-TR",
    "from_custodian": "张三", "to_custodian": "李四"})
r = batch(batch_body("BATCH-PRE", ["B-PRE"], eids=["PRE-E"], transfer_nos=["PRE-TR"]))
check("占用单事件接口既有事件号 409", r.status_code == 409
      and r.json()["error"]["code"] == "event_id_occupied", r.text)

# ---------------------------------------------------------------- 5. 批次查询与纠错追溯
print("5) 批次查询：逐份当前结论 + 原事件有效性；纠错只改有效链、归属可追溯")
reg("B-CX1"); reg("B-CX2")
r = batch(batch_body("BATCH-CX", ["B-CX1", "B-CX2"], note="车次京A-12345 同车移交"))
check("带备注批次 201 且备注留存",
      r.status_code == 201 and r.json()["note"] == "车次京A-12345 同车移交", r.text)

# 5.1 作废 B-CX1 的批次移交：有效链剔除，批次归属仍在、原事件标记失效
r = client.post(f"{API}/corrections", json={
    "correction_id": "CX-FIX-VOID", "sample_code": "B-CX1",
    "target_event_id": "BATCH-CX-E0", "action": "void",
    "operator": "质控员", "reason": "车次误交，作废该份移交"})
check("作废批次移交 201", r.status_code == 201, r.text)
b = get_batch("BATCH-CX").json()
v1 = item_of(b, "B-CX1")
v2 = item_of(b, "B-CX2")
check("作废份：原事件标记无效且指向纠错号/动作",
      not v1["original_event_effective"] and v1["superseded_by"] == "CX-FIX-VOID"
      and v1["correction_action"] == "void" and v1.get("replacement_event_id") is None, str(v1))
check("未纠错份：原事件仍有效",
      v2["original_event_effective"] is True and v2["superseded_by"] is None, str(v2))
check("作废份当前结论实时反映（无有效移交→缺入库/链不完整 pending）",
      v1["evaluation"]["status"] == "pending"
      and any("缺少实验室入库" in x or "缺少签收" in x for x in v1["evaluation"]["reasons"]),
      str(v1["evaluation"]))

# 批次原始归属仍可追溯：chain 的 superseded_events 中保留原事件及 batch_id
cx1chain = chain("B-CX1")
sup = [e for e in cx1chain["superseded_events"] if e["event_id"] == "BATCH-CX-E0"]
check("链查询 superseded_events 保留批次原事件与 batch_id 归属",
      len(sup) == 1 and sup[0]["batch_id"] == "BATCH-CX", str(sup))
check("有效时间线不再含已作废的批次移交",
      all(e["event_id"] != "BATCH-CX-E0" for e in cx1chain["timeline"]))

# 5.2 替换 B-CX2 的批次移交（新事件号）：批次查询指向替换件，原批次归属不动
r = client.post(f"{API}/corrections", json={
    "correction_id": "CX-FIX-REPL", "sample_code": "B-CX2",
    "target_event_id": "BATCH-CX-E1", "action": "replace",
    "operator": "质控员", "reason": "移交单号填错，整体替换",
    "new_event": {"event_id": "BATCH-CX-E1-FIX", "event_type": "transfer",
                  "occurred_at": "2026-09-30T02:10:00Z",
                  "transfer_no": "BATCH-CX-TR1-FIX",
                  "from_custodian": "张三", "to_custodian": "李四"}})
check("替换批次移交 201", r.status_code == 201, r.text)
b = get_batch("BATCH-CX").json()
v2 = item_of(b, "B-CX2")
check("替换份：原事件标记无效并指向替换新事件号",
      not v2["original_event_effective"] and v2["superseded_by"] == "CX-FIX-REPL"
      and v2["correction_action"] == "replace"
      and v2["replacement_event_id"] == "BATCH-CX-E1-FIX", str(v2))
check("批次归属行仍记录原始移交单号/原事件号（不被纠错改写）",
      v2["transfer_no"] == "BATCH-CX-TR1" and v2["event_id"] == "BATCH-CX-E1", str(v2))
cx2chain = chain("B-CX2")
check("有效链含替换件且带 replaces_event_id，原批次事件在 superseded",
      any(e["event_id"] == "BATCH-CX-E1-FIX" and e["replaces_event_id"] == "BATCH-CX-E1"
          for e in cx2chain["timeline"])
      and any(e["event_id"] == "BATCH-CX-E1" and e["batch_id"] == "BATCH-CX"
              for e in cx2chain["superseded_events"]),
      str((cx2chain["timeline"], cx2chain["superseded_events"])))

# 5.3 替换后核验按有效链推进并最终可核定；批次查询结论随之变为 valid
ev({"event_id": "B-CX2-R", "event_type": "receive", "sample_code": "B-CX2",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "BATCH-CX-TR1-FIX",
    "receiver": "李四", "seal_no": "SEAL-B-CX2"})
ev({"event_id": "B-CX2-S", "event_type": "storage", "sample_code": "B-CX2",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
r = client.post(f"{API}/samples/B-CX2/approve", json={"approver": "主管"})
check("替换后补齐链可核定 200", r.status_code == 200, r.text)
v2 = item_of(get_batch("BATCH-CX").json(), "B-CX2")
check("核定后批次查询逐份结论=valid 且原事件仍标记为已被替换（归属不变）",
      v2["evaluation"]["status"] == "valid" and v2["original_event_effective"] is False, str(v2))

# ---------------------------------------------------------------- 6. 请求体校验
print("6) 请求体校验")
bad = {"batch_id": "BX", "transfer_time": "2026-09-30T02:00:00Z",
       "from_custodian": "张三", "to_custodian": "李四", "items": []}
check("空 items 422", batch(bad).status_code == 422)
bad = {"batch_id": "BX", "transfer_time": "not-a-time",
       "from_custodian": "张三", "to_custodian": "李四",
       "items": [{"sample_code": "B-DUP", "transfer_no": "t", "event_id": "e"}]}
check("非法时间 422", batch(bad).status_code == 422)
bad = {"batch_id": "BX", "transfer_time": "2026-09-30T02:00:00Z",
       "from_custodian": "张三", "to_custodian": "李四",
       "items": [{"sample_code": "B-DUP", "transfer_no": "t", "event_id": "e"}],
       "unknown_field": 1}
check("多余字段 422（extra forbid）", batch(bad).status_code == 422)
bad = {"batch_id": "BX", "transfer_time": "2026-09-30T02:00:00Z",
       "from_custodian": "张三", "to_custodian": "李四",
       "items": [{"sample_code": "", "transfer_no": "t", "event_id": "e"}]}
check("空样本编号 422", batch(bad).status_code == 422)
check("校验失败不占用批次号", batch(
    {"batch_id": "BX", "transfer_time": "2026-09-30T02:00:00Z",
     "from_custodian": "张三", "to_custodian": "李四",
     "items": [{"sample_code": "B-DUP", "transfer_no": "t2", "event_id": "e2"}]}
).status_code == 201)

# ---------------------------------------------------------------- 7. 并发：批次 vs 核定（按已提交顺序判定）
print("7) 并发：批次提交与核定按已提交（写锁）顺序判定")
reg("B-RACE")
# 让该样本具备“一旦不再追加移交即可 valid 核定”的完整链（独立移交-签收-入库）
ev({"event_id": "RACE-T", "event_type": "transfer", "sample_code": "B-RACE",
    "occurred_at": "2026-09-30T01:30:00Z", "transfer_no": "RACE-TR-OLD",
    "from_custodian": "张三", "to_custodian": "李四"})
ev({"event_id": "RACE-R", "event_type": "receive", "sample_code": "B-RACE",
    "occurred_at": "2026-09-30T01:45:00Z", "transfer_no": "RACE-TR-OLD",
    "receiver": "李四", "seal_no": "SEAL-B-RACE"})
ev({"event_id": "RACE-S", "event_type": "storage", "sample_code": "B-RACE",
    "occurred_at": "2026-09-30T01:50:00Z", "custodian": "实验室"})
check("并发前置链 valid", chain("B-RACE")["status"] == "valid", str(chain("B-RACE")["reasons"]))

outcomes: dict[str, str] = {}
barrier = threading.Barrier(2)


def batch_worker():
    barrier.wait()
    # 新移交由当前保管人（实验室）发起、尚未签收：批次先提交则最终链出现“在途移交”
    # → pending，后到的核定必基于最终链判 422；核定先提交则批次被 history_locked 拒绝。
    body = batch_body("BATCH-RACE", ["B-RACE"], eids=["RACE-NEW-E"], transfer_nos=["RACE-NEW-TR"],
                      from_custodian="实验室", to_custodian="王五")
    outcomes["batch"] = str(batch(body).status_code)


def approve_worker():
    barrier.wait()
    outcomes["approve"] = str(client.post(
        f"{API}/samples/B-RACE/approve", json={"approver": "主管"}).status_code)


ta = threading.Thread(target=batch_worker); tb = threading.Thread(target=approve_worker)
ta.start(); tb.start(); ta.join(); tb.join()
print("   结局：", outcomes)
if outcomes["batch"] == "201":
    check("批次先提交：201", True)
    check("核定基于最终有效链拒绝 422（在途移交待补证）", outcomes["approve"] == "422", str(outcomes))
    ch = chain("B-RACE")
    check("最终链含批次新移交且为 pending",
          ch["status"] == "pending"
          and any(e["event_id"] == "RACE-NEW-E" for e in ch["timeline"]), str(ch["status"]))
else:
    check("核定先提交：200 冻结", outcomes["approve"] == "200", str(outcomes))
    check("批次因历史锁定失败 409 history_locked", outcomes["batch"] == "409", str(outcomes))
    check("失败批次不留痕", get_batch("BATCH-RACE").status_code == 404)
    check("最终保持核定 valid", chain("B-RACE")["status"] == "valid")

# ---------------------------------------------------------------- 8. 并发：两批次竞争事件号
print("8) 并发：两个批次竞争同一新事件号，恰一批成功且败者整批回滚")
reg("B-CA"); reg("B-CB")
out2: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def batch_w(tag: str, bid: str, code: str, other_code: str):
    barrier2.wait()
    body = batch_body(bid, [code, other_code],
                      eids=["SHARED-EID", f"{tag}-ONLY-E"],
                      transfer_nos=[f"{tag}-TR0", f"{tag}-TR1"])
    out2[tag] = batch(body).status_code


t1 = threading.Thread(target=batch_w, args=("A", "BATCH-CA", "B-CA", "B-CB"))
t2 = threading.Thread(target=batch_w, args=("B", "BATCH-CB", "B-CA", "B-CB"))
t1.start(); t2.start(); t1.join(); t2.join()
print("   结局：", out2)
check("恰一批 201、另一批 409", sorted(out2.values()) == [201, 409], str(out2))
con = sqlite3.connect(str(config.DB_PATH))
check("竞争事件号仅一条事件行",
      con.execute("SELECT COUNT(*) FROM custody_event WHERE event_id='SHARED-EID'").fetchone()[0] == 1)
winner = "A" if out2["A"] == 201 else "B"
loser = "B" if winner == "A" else "A"
check("败者批次头/归属行全部回滚",
      con.execute("SELECT COUNT(*) FROM transfer_batch WHERE batch_id=?", (f"BATCH-C{loser}",)).fetchone()[0] == 0
      and con.execute(
          "SELECT COUNT(*) FROM transfer_batch_item WHERE batch_pk="
          "(SELECT id FROM transfer_batch WHERE batch_id=?)", (f"BATCH-C{loser}",)).fetchone()[0] == 0)
check("败者独有事件不留存（无孤儿事件）",
      con.execute("SELECT COUNT(*) FROM custody_event WHERE event_id=?",
                  (f"{loser}-ONLY-E",)).fetchone()[0] == 0)
check("胜者批次完整（2 份归属 + 2 条事件）",
      con.execute(
          "SELECT COUNT(*) FROM transfer_batch_item WHERE batch_pk="
          "(SELECT id FROM transfer_batch WHERE batch_id=?)",
          (f"BATCH-C{winner}",)).fetchone()[0] == 2
      and con.execute("SELECT COUNT(*) FROM custody_event WHERE event_id IN ('SHARED-EID',?)",
                      (f"{winner}-ONLY-E",)).fetchone()[0] == 2)
con.close()

# 败者以未占用的新批次号/事件号重试可成功
retry_bid = f"BATCH-C{loser}-RETRY"
r = batch(batch_body(retry_bid, ["B-CA", "B-CB"],
                     eids=[f"{loser}-RETRY-E0", f"{loser}-RETRY-E1"],
                     transfer_nos=[f"{loser}-TR0", f"{loser}-TR1"]))
# 注意：B-CA/B-CB 已被胜者批次写入移交，再来一份移交会在核验层产生“在途再次移交”（invalid），
# 但批次写入本身只校验占用/核定/重复，业务链结论由重算体现——此处第二份移交号不同、仍在途，
# 故提交应成功（201），结论转 invalid。
check("败者换新号重试提交成功（核验结论由链重算体现 invalid）",
      r.status_code == 201 and r.json()["items"][0]["evaluation"]["status"] == "invalid", r.text)

# ---------------------------------------------------------------- 9. 既有接口行为不变
print("9) 既有接口与字段保持兼容")
r = get_batch("BATCH-1")
check("批次查询 200 且结构稳定",
      r.status_code == 200
      and set(r.json().keys()) == {"batch_id", "transfer_time", "from_custodian", "to_custodian",
                                   "note", "created_at", "total", "items"})
ch = chain("B-S2")
for key in ("sample", "status", "reasons", "current_custodian", "pending_transfer",
            "has_storage", "approved_storage_at", "timeline", "corrections", "superseded_events"):
    check(f"chain 保留字段 {key}", key in ch)
check("非批次事件 timeline 的 batch_id 为 null",
      all(e["batch_id"] is None for e in chain("B-PRE")["timeline"]
          if e["event_id"] == "PRE-E"))
# 纠错接口行为不变：对批次事件的纠错与普通事件一致（已在上文 201/留痕 覆盖），
# 核定后对批次事件再纠错仍 history_locked
r = client.post(f"{API}/corrections", json={
    "correction_id": "CX-AFTER-LOCK", "sample_code": "B-S1",
    "target_event_id": "BATCH-1-E0", "action": "void",
    "operator": "质控员", "reason": "核定后试探"})
check("核定后对批次事件纠错仍 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
