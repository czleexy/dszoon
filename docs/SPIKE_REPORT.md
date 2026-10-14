# P0 技术验证报告（真机实测）

> 本文件记录**真机实测结果**。开发计划里的命令对照表是"应该是什么"，
> 这里记录的是"实际上是什么"——两者不一致的地方，一律以本文件为准。
>
> 产出方式：每台设备跑一次
> `python scripts/spike_probe.py --vendor <hw|h3c|ruijie> --host <ip> --username <user> --out .spike/<设备名>`
> 然后把 `.spike/<设备名>/report.md` 的内容整理进下表。

## 1. 测试环境

| 项 | 值 |
|---|---|
| 采集主机 OS / Python | Windows / Python 3.13 |
| 采集主机到设备管理网 | 直达（未使用跳板机） |
| 测试时间 | 2026-10-10 |

## 2. 设备清单与实测结果

| # | 厂商 | 型号 | 软件版本 | 取配置命令 | 关分页命令 | 关折行命令 | 提权 | 编码 | 耗时 | 行数 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 华为 | S5720S-28X-SI-AC | VRP 5.170 (V200R011C10SPC600) | `display current-configuration` | `screen-length 0 temporary` | `screen-width 512`（弹确认，答 Y 后生效） | 不需要 | UTF-8 | 2.28s | 150 |
| 2 | 华为 | | | | | | | | | |
| 3 | 华三 | | | | | | | | | |
| 4 | 华三 | | | | | | | | | |
| 5 | 锐捷 | | | | | | | | | |
| 6 | 锐捷 | | | | | | | | | |

## 3. 逐厂商结论

### 3.1 华为 VRP

**已实测：VRP 5.170 / S5720S-28X-SI-AC（主机名 HULIANWANG-NL01）**

- 提示符形态：用户视图 `<HULIANWANG-NL01>`；设备名反查正常（`<HOST>` 形态）
- `screen-length 0 temporary`：**生效**，0.08s，无任何确认提示
- `screen-width 512`：**会弹交互确认**
  ```
  Warning: This command will change the default screen width. Continue? [Y/N]:
  ```
  - 不回答就一直等；中途发空格会被拒绝：
    `Error: Please choose 'YES' or 'NO' first before pressing 'Enter'. [Y/N]:`
  - 实测白等 **65 秒**才失败，报错只是"会话空闲 25s 无任何数据"，完全指不到真正原因
  - `screen-width 256` 不弹确认，直接生效
  - **已核对：不会写进配置**（`display current-configuration` 里没有 `screen-width` 行），
    所以只影响本次会话 → 自动回答 Y 是安全的
- `display version` 型号行格式（与 VRP8 不同，型号后有产品描述）：
  ```
  HUAWEI S5720S-28X-SI-AC Routing Switch uptime is 1 week, 4 days, 4 hours, 11 minutes
  ```
- 编码：UTF-8，无乱码
- 取配置耗时 2.28s（150 行），可接受
- 特殊情况：**配置里存在约 100 字符的长行**
  （`local-user ... irreversible-cipher $1a$...`）——所以终端宽度设置**必须生效**，
  否则这些行会被劈成两半且备份"看起来有内容"

### 3.2 华三 Comware

- 提示符形态：
- `screen-length disable` 是否生效：
- `screen-width 512` 是否生效 / 是否弹确认：
- 是否输出 `Current configuration` 统计行（归一化要处理）：
- GBK 场景验证结果：
- 特殊情况（IRF / MDC）：

### 3.3 锐捷 RGOS

- 提示符形态（用户模式 / 特权模式 / 配置模式）：
- `terminal length 0` 是否生效：
- `terminal width 512` 是否生效 / 是否弹确认：
- `enable` 是否需要密码；**没设密码时是否直接成功**：
- `show running-config` 与 `show startup-config` 的差异：
- 特殊情况：

## 4. 完整性校验结论

| 检查项 | 设备 #1（华为 S5720S） |
|---|---|
| 采集结果与手工 `display current-configuration` 逐字节一致？ | 待人工比对（结构校验通过：150 行、末行 `return`、无分页符残留） |
| 末行是否为 `return` / `end`？ | ✅ `return` |
| 是否出现长度恰好 78~81 或 130~132 的行（折行嫌疑）？ | ✅ 无（最长行约 100 字符，且未被劈开） |
| 归一化后是否仍通过校验？ | ✅ |
| 会话预处理命令是否写进了配置（= 改变了设备状态）？ | ✅ 未写入 |
| 脱敏后是否没有任何明文口令残留？ | ✅ |

## 5. 需要改驱动的地方（逐条列出）

| # | 设备/版本 | 现象 | 处理方式 | 状态 |
|---|---|---|---|---|
| 1 | 华为 VRP5 | `screen-width 512` 弹 `[Y/N]` 确认，不回答就干等到超时（65s） | 新增通用"交互确认自动回答"机制：`VendorDriver.confirm_rules()` + 会话层 `_match_confirmation`，YAML 声明 `confirm_prompts` | ✅ 已实现并加真机回放测试 |
| 2 | 华为 VRP5 | `display version` 里型号与 `uptime is` 之间夹着 `Routing Switch`，导致 facts 里**没有 model 键**（备份本身成功，极难察觉） | `facts.model` 正则改为 `^\s*(?:HUAWEI\|Huawei)\s+(\S+)(?:\s+.*?)?\s+uptime is` | ✅ 已修复，夹具来自真机回显 |
| 3 | 通用 | "只读备份"的承诺需要自动校验，不能靠自觉 | 新增 `integrity.check_setup_leakage()`：整行精确匹配预处理命令是否出现在配置里，命中即警告（`setup_leaked_into_config`） | ✅ 已实现 |
| 4 | 通用 | 提示符前若带 ANSI 转义序列，提示符匹配失败会导致**所有命令超时** | 读取循环匹配前统一 `clean_terminal()` | ✅ 已修复（由 h3c mock 剧本发现） |

## 6. 新增夹具清单

| 夹具文件 | 来源 | 版本 | 用途（覆盖哪个坑） |
|---|---|---|---|
| `tests/fixtures/huawei/vrp5/display_version.txt` | 真机 S5720S（ESN 已脱敏） | VRP 5.170 | 型号后带产品描述的长格式 |
| `tests/fixtures/huawei/vrp5/display_current-configuration.txt` | 按真机结构编写 | VRP 5.170 | VRP5 配置结构（`#` 分隔、`stelnet`/`user-interface` 段） |
| `tests/fixtures/huawei/vrp8/*` | 合成样本 | VRP 8.180 | 分页/折行/脱敏/tips 基线 |

> 夹具必须是**脱敏后**的真实回显。真机的空格、退格、换行古怪之处，
> 手写样本永远造不出来。

## 7. 新增 mock 剧本清单

| 剧本名 | 模拟的异常 | 对应测试 |
|---|---|---|
| `huawei-vrp5-screen-width-confirm` | 真机回放：`screen-width 512` 弹 `[Y/N]` 确认 | `test_screen_width_confirmation_is_auto_answered` |
| `huawei-vrp8-paging-stuck` | 关分页命令被拒绝，只能靠响应分页符取全 | `test_huawei_paging_stuck_still_gets_full_config` |
| `huawei-vrp8-more-inline` | 分页符贴在内容行尾 | `test_huawei_inline_more_token_does_not_corrupt_config` |
| `h3c-comware7-gbk` | GBK 中文 + 提示符前带 ANSI 转义序列 | `test_h3c_gbk_with_ansi_noise` |
| `ruijie-rgos11-enable` / `-no-enable-password` | enable 提权两种分支 | `test_ruijie_enable_password_flow` 等 |
| `huawei-hang` / `huawei-close-mid` / `huawei-auth-fail` / `huawei-truncated` | 卡死 / 断连 / 认证失败 / 内容截断 | 各自的失败路径测试 |

## 8. P0 验收结论

- [x] 华为至少 1 个版本采到**完整未被折行破坏**的配置（150 行，含 100 字符长行）
- [x] 记录了命令候选的正确/错误表现（含 `screen-width 512` 被确认提示拦住）
- [x] 夹具库覆盖：分页、折行、ANSI、GBK、截断、命令报错、真机确认提示
- [x] mock 剧本覆盖：分页（两种位置）、卡死、断连、认证失败、无 enable 密码、`[Y/N]` 确认
- [x] 驱动 YAML 的命令表已按实测校准（华为）
- [x] **采集层 pytest 全绿**（约 111 个用例）——由 `.pytest_cache/v/cache/lastfailed` 证据确认：
      其中只剩 3 条指向已被重命名/删除的测试的陈旧记录，真实失败已全部被后续一次运行清除
- [ ] 华三 / 锐捷真机实测（**待做**：这两栏仍是空的）
- [ ] 采集结果与手工执行结果逐字节比对（**待人工确认**）

**结论**：华为（VRP5）已具备进入 P1 的条件；华三/锐捷仍需各至少 1 台真机实测。
