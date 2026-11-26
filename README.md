# 共用实验室送检与复测承诺

联调数据采用 [《样品接收与仪器回报》](交换约定.md) 的字段和时间口径。`data/samples_20261208.json` 是各参与方的合成报文快照，含未核认、互相冲突或处于变化中的来源事实。

使用 Python 3.11 标准库。执行 `python3 -m unittest discover -s tests -v` 检查交换资料和本地读取。需要给本机接入方提供样本时，可运行以下只读报文服务。

```sh
python3 -m http.server 8040 --bind 127.0.0.1 --directory data
```

在同一环境读取 `http://127.0.0.1:8040/samples_20261208.json` 可取得 UTF-8 JSON。JSON 的缺省字段以 `null` 表示。

## 本地联调引擎（仅标准库）

`labdesk/` 按《样品接收与仪器回报》实现样品数量分层、原子占用、分取追溯、
受理窗口、校准影响、报告效力与复测三分类。常用命令：

```sh
python3 -m unittest discover -s tests -v           # 交换资料 + 全部业务规则
python3 -m labdesk analyze --write out/decision_20261208.json
python3 -m labdesk board --at 14:20                # 可以安排/缺留样/等仪器回报
python3 -m labdesk order O11 --at 14:20
python3 -m labdesk report R1
python3 -m labdesk serve --port 8040               # API + 看板 / 与 data 报文
```

启动 `serve` 后：

- 看板 <http://127.0.0.1:8040/>
- `GET /api/analysis`、`/api/board?at=14:20`、`/api/orders?at=`、
  `/api/order/O11?at=14:20`、`/api/reports`、`/api/report/R1`、
  `/api/quantities`、`/api/impacts`
- `POST /api/message` 投递交换报文或同试验编号的更正回执（原回执保留），
  `POST /api/accept`、`POST /api/occupy` 做原子受理/占用。

快照的逐项核对答案见 [核对结论.md](核对结论.md)，机器可读结论见
`out/decision_20261208.json`。作为纯静态报文服务时，仍可使用上面的
`python3 -m http.server 8040 --bind 127.0.0.1 --directory data`。
