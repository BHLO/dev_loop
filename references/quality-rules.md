# 实现质量规则（`quality` 命令判据）

`quality` 只看**本次改动新增的行**，不翻存量代码——存量问题归 S4 健康检查。
返回码：有 P0 → 2，只有 P1 → 1，干净 → 0。

## 一、分级

| 级别 | 含义 | 处理 |
|---|---|---|
| **P0** | 安全或正确性硬伤 | **必须改完**才能进 S3/S4，不许豁免 |
| **P1** | 明显该改，但可讨论 | 本次改完，或在 S2 报告里逐条写豁免理由 |
| **P2** | 建议 | 可排期，记录即可 |

## 二、P0 规则

| ID | 类别 | 判据 |
|---|---|---|
| SEC-01 | 硬编码密钥 | `AKIA…`、`sk-…`、`ghp_…`、`xox[baprs]-…`、`BEGIN … PRIVATE KEY` |
| SEC-02 | 硬编码口令 | `password` / `secret` / `token` / `api_key` 直接赋字符串常量 |
| SEC-03 | SQL 拼接 | `execute(...)` 里用 `+` / f-string 拼；或含 `SELECT/INSERT/UPDATE/DELETE/DROP/ALTER` 的字符串常量参与拼接 |
| SEC-04 | 动态执行 | `eval(` / `exec(` / `new Function(` |
| SEC-05 | 关掉校验 | `--no-verify`、`verify=False`、`InsecureSkipVerify`、`rejectUnauthorized:false`、`chmod 777` |
| ERR-01 | 吞异常 | `except …: pass`（含跨行写法）——异常被吃掉，出错无人知晓 |

## 三、P1 规则

| ID | 类别 | 判据 |
|---|---|---|
| ERR-02 | 空 catch | `catch (e) { }` |
| DBG-01 | 调试残留 | `print(`、`console.log/debug`、`debugger`、`fmt.Print*`、`System.out.print`、`var_dump(` |
| DBG-02 | 新增 TODO | `TODO` / `FIXME` / `XXX` / `HACK` |
| DBG-03 | 硬编码地址 | `localhost`、`127.0.0.1`（含端口） |
| DBG-07 | 阻塞等待 | `time.sleep(` / `Thread.sleep(` / `setTimeout(`（测试文件除外） |
| TST-01 | 测试缺口 | 改了实现代码，但本次**没有任何测试文件改动** |

## 四、P2 规则

| ID | 类别 | 判据 |
|---|---|---|
| DBG-04 | 超长行 | 新增行超过 160 字符 |
| DBG-05 | 类型忽略 | `# type: ignore`、`@ts-ignore`、`eslint-disable` |
| DBG-06 | 注释掉的代码 | 连续 3 行以上以 `#` / `//` 开头的代码 |
| SIZ-01 | 改动过大 | 单文件新增超过 400 行 |
| SIZ-02 | 函数过长 | 新增的 Python 函数超过 80 行 |

## 五、豁免怎么写

P1/P2 可以不改，但必须在 S2 报告的「实现质量」一节里逐条写：

```
- DBG-01 src/api/refund.py:10：该 print 是运维要求的审计输出，将改为 logger.info，issue #123
- TST-01 src/service/order.py：本次仅改签名透传，行为不变，由 test_refund.py 覆盖
```

**P0 不允许豁免。** 确实要放过（例如密钥是测试用的假值），改为在规则里加白名单，
并在提交信息里说明——不要靠口头豁免。

## 六、它测不到什么

这份规则只抓「一眼能判定」的写法问题。以下必须人看：

- 命名是否达意、抽象是否合理、分层是否被破坏
- 边界条件、并发、事务边界、幂等
- 错误信息是否对用户有用
- 这段逻辑到底对不对

`quality` 通过 ≠ 代码写得好，只等于「没有常见硬伤」。
