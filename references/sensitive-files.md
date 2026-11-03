# 敏感文件清单与确认协议

改动前逐条比对。命中即按 SKILL.md §十 的协议确认，**未获明确同意不得修改**。

---

## 一、高危（逐文件单独确认）

| 类别 | 典型路径 / 文件名 | 为什么危险 |
|---|---|---|
| 数据库迁移 | `migrations/`、`migrate/`、`db/migrate/`、`alembic/versions/`、`*.sql` | 不可逆、可能锁表、存量数据受损 |
| 密钥与证书 | `*.pem`、`*.key`、`*.p12`、`*.jks`、`id_rsa*`、`.env*`、`secrets.*` | 泄露即事故；轮换成本极高 |
| CI/CD | `.github/workflows/`、`.gitlab-ci.yml`、`Jenkinsfile`、`azure-pipelines.yml`、`.circleci/` | 影响所有人和所有分支 |
| 权限与鉴权 | `auth/`、`security/`、`permission*`、`rbac*`、`acl*`、`middleware/auth*` | 改错等于开门；回归极难发现 |
| 生产配置 | `application-prod.*`、`config/prod*`、`values-prod.yaml`、`*.prod.env` | 影响线上行为，本地测不出 |
| 发布与回滚 | `deploy/`、`release*`、`rollback*`、`scripts/deploy*` | 出问题无法回滚 |
| 依赖锁文件 | `package-lock.json`、`pnpm-lock.yaml`、`yarn.lock`、`poetry.lock`、`go.sum`、`Cargo.lock` | 隐式升级依赖，可能引入破坏性变更 |

## 二、中危（可批量确认一次，但必须列全）

| 类别 | 典型路径 | 风险点 |
|---|---|---|
| 公共基础库 | `common/`、`utils/`、`core/`、`shared/`、`lib/`（被多方引用） | 一处改动波及全仓 |
| 依赖清单 | `package.json`、`pom.xml`、`requirements.txt`、`pyproject.toml`、`go.mod`、`Cargo.toml` | 影响构建与运行环境 |
| 容器与编排 | `Dockerfile*`、`docker-compose*`、`k8s/`、`helm/`、`*.yaml`（部署类） | 环境差异导致"本地能跑线上不行" |
| 构建配置 | `webpack.config.*`、`vite.config.*`、`tsconfig.json`、`babel.config.*`、`Makefile` | 影响产物与所有人 |
| 消息与调度 | `mq/`、`topics*`、`scheduler/`、`cron*`、任务定义文件 | 重复消费、漏消费、重复调度 |
| 缓存与 session | `cache/`、`session*`、`redis*` | key 结构变化引发脏读 |

## 三、低危（无需确认）

普通业务代码、样式、文案、测试代码、`docs/`、注释。按 S1 清单直接执行。

---

## 四、内容特征兜底

路径没命中但内容命中，同样按高危处理：

```
-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----
AKIA[0-9A-Z]{16}                      # AWS Access Key
ghp_[A-Za-z0-9]{36}                   # GitHub Token
sk-[A-Za-z0-9]{20,}                   # 通用 API Key
password\s*[:=]\s*["'][^"']+["']     # 硬编码口令
jdbc:[a-z]+://[^"\s]+                 # 生产连接串
```

命中任一，先在报告里标红，再问。

---

## 五、判断流程

```
要改一个文件
  ├─ 路径命中高危 → 停下，单文件确认
  ├─ 路径命中中危 → 攒齐本批次的中危文件，一次列全确认
  ├─ 内容命中密钥特征 → 按高危处理
  └─ 都不命中 → 直接按清单改
```

**拿不准就按敏感处理。** 多问一句的成本，远低于改错一次迁移脚本。

---

## 六、确认记录

每次获得授权，在对应阶段产物末尾追加一行：

```
| 时间 | 文件 | 级别 | 用户原话 | 变更摘要 |
|---|---|---|---|---|
| 2026-11-03 10:12 | migrations/0031.sql | 高危 | "可以，按这个改" | 加 refund_amount 字段 + 回填 |
```

下一轮再动同一文件，需要重新确认——授权不继承。

---

## 七、机器可读规则（脚本读这一段）

`scripts/devloop.py` 的敏感判定直接读下面这个块，**这是唯一的事实源**——
改规则只改这里，不要再去脚本里改数组。表格（§一、§二）是给人看的说明，脚本不读。

修改后跑一次自检：`python tests/test_skill_rules.py`

```yaml
rules:
  - level: 高危
    label: 数据库迁移
    pattern: '(^|/)(migrations?|alembic/versions)/'
  - level: 高危
    label: SQL 脚本
    pattern: '\.sql$'
  - level: 高危
    label: 密钥/证书
    pattern: '\.(pem|key|p12|jks)$'
  - level: 高危
    label: 环境配置
    pattern: '(^|/)\.env'
  - level: 高危
    label: CI/CD
    pattern: '(\.github/workflows/|\.gitlab-ci\.yml|Jenkinsfile|azure-pipelines)'
  - level: 高危
    label: 权限鉴权
    pattern: '(^|/)(auth|security)/|(permission|rbac|acl)'
  - level: 高危
    label: 生产配置
    pattern: '(prod|production)[\w.-]*\.(ya?ml|properties|env)$'
  - level: 高危
    label: 发布脚本
    pattern: '(^|/)(deploy|release|rollback)'
  - level: 高危
    label: 依赖锁文件
    pattern: '(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|go\.sum|Cargo\.lock)'
  - level: 中危
    label: 公共基础库
    pattern: '(^|/)(common|utils|core|shared|lib)/'
  - level: 中危
    label: 依赖清单
    pattern: '(package\.json|pom\.xml|requirements\.txt|pyproject\.toml|go\.mod|Cargo\.toml)$'
  - level: 中危
    label: 容器与编排
    pattern: '(Dockerfile|docker-compose|(^|/)k8s/|(^|/)helm/)'
```

**加一条规则的步骤**：在块里追加 `- level / label / pattern` 三项，pattern 用单引号包住，
反斜杠写两个；再在上面表格补一行说明。脚本下次运行就生效。
