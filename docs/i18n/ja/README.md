[English](../../../README.md) | [한국어](../ko/README.md) | 日本語

# korea-persona-interview

[![CI](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml/badge.svg)](https://github.com/binaryloader/korea-persona-interview/actions/workflows/test.yml)

OpenAI、Anthropic Claude、または OpenAI 互換のローカル LLM(mlx_lm.server、vLLM、llama.cpp)上で韓国人合成ペルソナインタビューを実行するための実用的な CLI です。NVIDIA Nemotron-Personas-Korea データセット(CC BY 4.0、約100万の韓国人合成ペルソナ)を任意のモデルと組み合わせ、実際の参加者を募集する前に製品アイデア、インタビューガイド、ペルソナ仮説をプレッシャーテストします。

このツールは4つの CLI サブコマンド(`healthcheck`、`list-personas`、`interview`、`report`)、マシン間連携用の JSON 出力モード、そして MCP(Model Context Protocol)エントリポイントを提供します。MCP エントリポイントは MCP サーバーモード(サーバーサイドの OpenAI/Anthropic 呼び出し)または MCP オーケストレーターモード(ホストエージェントのサブエージェントが LLM 処理を担当)のいずれかで動作します。

## Features

- 100万件以上の韓国人合成ペルソナ(NVIDIA Nemotron-Personas-Korea、CC BY 4.0)を用いたマルチターンインタビュー
- 3種類の推論先：OpenAI Chat Completions API、Anthropic Messages API、任意の OpenAI 互換ローカルサーバー
- 並行度1〜10、tqdm 進捗表示、SIGINT 部分保存、終了コード3の部分失敗検出を備えた非同期バッチランナー
- 性別/年齢/地域/家族構成軸に対する文単位の一人称主張に基づくペルソナドリフト検出(否定ガード、三人称除外)と英語比率セーフティネット
- `--persona-id` で uuid を指定し特定のペルソナを固定して A/B 比較、`--resume PATH` で前回バッチの失敗レコードのみ再実行
- `--insight-model` でインタビューを小さなモデルで、定性インサイトの呼び出しをより大きなモデルで実行可能
- OpenAI ストリーミング(`llm.streaming: true`)と Anthropic プロンプトキャッシュ(`llm.anthropic_cache_control: true`、デフォルトで有効)
- 偽陽性をクリアするための LLM-as-judge ドリフト精緻化(`heuristics.llm_drift_review`、オプトイン)
- すべての構造化サマリーに `acceptable_price_signal`(`cheap`/`fair`/`expensive`/`null`)が含まれ、シグナル分布から WTP 推奨を任意で算出します
- Claude Code、Cursor、Codex 用の MCP エントリポイント(`python -m src.mcp_server`)。`mcp.mode` で `orchestrator`(デフォルト、サーバーサイドキー不要)と `server`(サーバーサイドの OpenAI/Anthropic 呼び出し)を切り替えます
- 実行ごとにマークダウンレポートを自動生成(`--no-report` で無効化)、シェルスクリプト用の `--json` ルートモード
- すべての質問を1回のチャット呼び出しにまとめてトークンを節約するシングルターンモード(`--single-turn`)
- 各実行の最後にトークン使用量(prompt / completion / cached)を出力し、JSON とレポートヘッダにも埋め込みます
- `--seed` による再現可能なサンプリング。同じ seed、同じフィルター、同じデータセットバージョンであれば同じペルソナを返します
- 運用面の強化：ログ内のペルソナ id を sha256 でマスキング、`outputs/` をモード0700で作成(結果ファイルは0600)、`--product` と各質問テキストに2000文字制限とプロンプトインジェクションガードを適用
- 外部テレメトリなし。外向き通信は設定された LLM エンドポイントと(初回実行時)データセット用の Hugging Face Hub のみです

## Requirements

- Python 3.12(`.python-version` で固定)
- [uv](https://docs.astral.sh/uv/) パッケージマネージャー
- 利用するプロバイダーに応じた API キー
  - `provider=openai`(デフォルト)では `OPENAI_API_KEY`。https://platform.openai.com/api-keys から発行します
  - `provider=anthropic` では `ANTHROPIC_API_KEY`。https://console.anthropic.com/ から発行します
  - ローカル LLM(mlx_lm.server、vLLM、llama.cpp)では `provider=openai` を維持し、空でない任意の値を使用します
- LLM API 呼び出しと初回データセットダウンロード(約100万レコード、以降 `~/.cache/huggingface` にキャッシュ)のためのインターネット接続
- macOS、Linux、Windows のすべてに対応します。Apple Silicon、GPU、ローカルランタイムは不要です

## Installation

`.python-version` が Python 3.12 を固定しているため、`uv venv` が自動的に適切なインタプリタを選択します。本番デプロイでは環境間で解決済みグラフを同一に保つため、lockfile からインストールする必要があります。

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
```

`requirements*.txt` を編集した後は lockfile を再コンパイルします。

```bash
uv pip compile requirements.txt -o requirements.lock
uv pip compile requirements-dev.txt -o requirements-dev.lock
```

CLI を `kpi` として、MCP サーバーを `kpi-mcp-server` としてどこからでも実行するには、依存関係の同期後にプロジェクトを editable モードでインストールします。

```bash
uv pip install -e .
```

uv が使えない場合は通常の pip でも動作します。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

直接的なランタイム依存関係は `pyproject.toml`(`[project.dependencies]`)にあります。公式の `openai`、`anthropic` SDK は意図的に使用していません。呼び出しは `httpx` 経由で行うため、依存関係ツリーが小さく保たれ、再試行、タイムアウト、ロギングのポリシーをプロジェクトが直接所有できます。背景は [docs/adr/2026-05-02-openai-backend-migration.md](../../adr/2026-05-02-openai-backend-migration.md) を参照してください。

## Quick Start

5つのコマンドで、新規チェックアウトから完成したレポートまで到達します。最初のインタビュー実行ではデータセットをダウンロードします(5〜10分)。以降の実行は30秒以内に開始します。

```bash
export OPENAI_API_KEY=sk-...
python main.py healthcheck
python main.py list-personas --filter "age:25-39,region:서울특별시" --limit 20
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
python main.py report outputs/interview_korea-persona-interview_20260502_120000.json
```

`interview` コマンドはマークダウンレポートを自動生成します(デフォルト `--report`)。単独の `report` ステップは `--no-report` を使用した、JSON を編集した、または異なる `--top-n`/`--include-drift` 設定で再生成したい場合のみ必要です。

プロジェクトルートの `.env` ファイルに `OPENAI_API_KEY=sk-...`(または `ANTHROPIC_API_KEY=sk-ant-...`)を置くと自動的に読み込まれます。すでに設定されたシェル環境変数が `.env` よりも優先されます。

Claude を使用するには `ANTHROPIC_API_KEY` を設定し、`--provider anthropic` を渡します。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py interview --provider anthropic --model claude-haiku-4-5 --product "..." --questions "..." --n 10
```

ローカルの OpenAI 互換サーバーを使用するには、`provider=openai` を維持して `--base-url` を上書きします。空でない `OPENAI_API_KEY` であれば何でも動作します。ローカルサーバーは値を無視します。

```bash
export OPENAI_API_KEY=local
python main.py interview --base-url http://localhost:8080/v1 --model llama-3-8b --product "..." --questions "..." --n 10
```

## Usage Examples

### Validate a product idea

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원, 주 2회 배송" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "이 서비스 쓰실 의향 있나요?" "월 얼마면 적당한가요?" "거절한다면 왜요?"
```

意向率(肯定/中立/否定)、価格受容額の中央値と IQR、上位の拒否理由、次のラウンドに向けた5〜10件の実行可能なインサイトを含むマークダウンレポートが生成されます。

### A/B test product copy on the same personas

最初のバッチからペルソナ id を取り出し、2回目の実行でリプレイすることで、2つの実行に同一のペルソナ id を固定します。

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬, 월 39,900원" --filter "age:25-39,region:서울특별시" --n 10 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-a/

python -c "import json,sys; d=json.load(open(sys.argv[1])); print('\n'.join(r['persona_id'] for r in d['records']))" outputs/copy-a/interview_*.json > /tmp/persona_ids.txt

xargs -I {} echo --persona-id {} < /tmp/persona_ids.txt | xargs python main.py interview --product "주말에 받는 1주일치 한식 반찬 박스, 월 39,900원" --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/copy-b/
```

両方の実行が正確に同じペルソナ id をインタビューするため、唯一の変数は製品コピーです。

### Cohort comparison

```bash
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:20-29" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-20s/
python main.py interview --product "직장인 1인 가구를 위한 건강 반찬 정기배송" --filter "age:30-39" --n 15 --seed 42 --questions "쓸 의향?" "월 얼마면?" "거절 사유?" --output outputs/cohort-30s/
```

各レポート内のコホート意向率テーブルは地域と性別でさらに分割されるため、20代/30代の差がすべての地域で維持されているのか、それとも一部のセグメントから来ているのかを確認できます。

### Large-scale screen with single-turn mode

シングルターンモードはすべての質問を1回のチャット呼び出しにまとめます。これによりマルチターンに対してプロンプトトークンがおよそ半減します。このモードでは自動フォローアップは無効です。

```bash
python main.py interview --product "1인 가구용 반찬 정기배송, 월 39,900원" --filter "age:20-49" --n 100 --seed 42 --concurrency 8 --single-turn --questions "이 서비스 쓸 의향?" "월 얼마면 적당?" "거절 사유?"
```

### Resume after a partial-failure exit

30人バッチが rate-limit の集中で終了コード3で終了したとします。前回の JSON の上に失敗レコードのみを再実行します。

```bash
python main.py interview --product "..." --filter "..." --n 30 --seed 42 --questions "..." --resume outputs/interview_korea-persona-interview_20260502_120000.json
```

`meta_extra.previous_run_id` が元の `interview_id` に設定されるため、2つの実行を関連付けられます。

### Tip: ask explicit value-pricing questions

`willingness_to_pay` はペルソナが具体的な数字を述べた場合のみ埋められます。明示的な数字の比率を最大化するには、直接的な価値-価格の質問を行います。

- "본인은 월 얼마면 가입하시겠어요?"(月額サブスクリプションへのアンカー)
- "월 39,900원이면 가입할 의향이 있으세요? 아니면 얼마면 적당할까요?"(逆提案プロンプト)
- "비슷한 서비스에 한 달에 얼마까지 쓸 수 있어요?"(上限の探索)

オープンエンドな価格質問はしばしば定性シグナル(`acceptable_price_signal`)のみを返します。このシグナルはすべてのレコードに埋められますが、`willingness_to_pay` の整数を生成するわけではありません。

## CLI Reference

### Subcommands

| Command | Description | Exit codes |
| --- | --- | --- |
| `healthcheck` | プロバイダーへの到達性とモデルの利用可能性を検証します | 0 ok, 1 missing key / 401 / 429 / unreachable |
| `list-personas` | フィルターに一致するペルソナをプレビューします | 0 ok, 2 no match |
| `interview` | バッチインタビューを実行し、JSON を保存してレポートを自動生成します | 0 ok, 1 server error, 2 sample shortfall, 3 partial failure |
| `report` | インタビュー JSON からマークダウンレポートを生成します | 0 ok, 1 input error, 2 no valid records |

終了コード130は `SIGINT`(Ctrl-C)用に予約されています。最初の割り込みは部分 JSON を保存し、2回目の割り込みは即座に終了します。

### Root options

これらはすべてのサブコマンドに適用され、サブコマンド名の前に置く必要があります。

| Option | Default | Description |
| --- | --- | --- |
| `--config PATH` | cwd の `config.yaml` | 設定ファイルパスを上書きします |
| `--no-color` | off | ANSI カラー出力を無効化します(`NO_COLOR` 環境変数も尊重します) |
| `--log-level LEVEL` | yaml 由来の `INFO` | ログレベルを設定します：`DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `--json` | off | stdout に単一の JSON ドキュメントを出力します。tqdm、カラー、韓国語ラベルを無効化します。エラーは `{"error": {...}}` として出力され、終了コードは0以外になります |

### `interview` options

| Option | Default | Description |
| --- | --- | --- |
| `--product TEXT` | required | 1行の製品説明(最大2000文字) |
| `--questions TEXT` | required, repeatable | 各質問は1つの `--questions` フラグです(各最大2000文字) |
| `--filter SPEC` | none | フィルター DSL(下記参照) |
| `--persona-id UUID` | none, repeatable | uuid で特定のペルソナ id を固定します。`--n` と `--seed` のランダム化を無効化します。`--filter` と組み合わせると交差になります |
| `--n N` | `10` | ペルソナ数 |
| `--seed N` | `42` | サンプリング seed |
| `--concurrency N` | `4` | 非同期並行度、範囲1〜10 |
| `--persona-fields LIST` | `summary` | カンマ区切りトグル：`summary`、`professional`、`sports`、`arts`、`travel`、`culinary`、`family` |
| `--follow-up TEXT` | none, repeatable | すべてのペルソナに共通の追加質問 |
| `--single-turn` | off | すべての質問を1回のチャット呼び出しにまとめます。自動フォローアップは無効化されます |
| `--dry-run` | off | ペルソナ1人だけ実行してコンソールに出力し、JSON とレポートのいずれも書き出しません |
| `--output DIR` | `outputs/` | 結果 JSON ディレクトリ |
| `--report / --no-report` | `--report` | インタビュー後にマークダウンレポートを自動生成します |
| `--resume PATH` | none | 前回の結果 JSON の `failed` レコードのみを再実行します |
| `--provider {openai,anthropic}` | `llm.provider` から | LLM プロバイダー |
| `--base-url URL` | `llm.base_url` から | LLM サーバーの base URL |
| `--model MODEL_ID` | `llm.model` から | 一回限りのモデル上書き |

### `report` options

| Option | Default | Description |
| --- | --- | --- |
| `RESULT_PATH` | required(positional) | インタビュー JSON のパス |
| `--top-n N` | `10` | 上位拒否理由の数 |
| `--include-drift` | off | 定量集計に `status: drift` のレコードを含めます |
| `--output-dir DIR` | 入力 JSON の隣 | マークダウンレポートを保存する場所 |
| `--insight-model MODEL_ID` | `common.report.insight_model` または `--model` から | 定性インサイト呼び出しのみ別のモデルを使用します |

`healthcheck` と `list-personas` は同じ provider/base-url/model のトリオに加え、filter/limit/seed を受け取ります。完全な一覧は `python main.py {sub} --help` で確認してください。

### Filter DSL

フィルターはカンマで区切られた `key:value` ペアを使用します。異なるキー同士は AND で結合し、同じキーが繰り返されると OR で結合します。

- `age:25-39`(範囲)、`age:30`(完全一致)
- `gender:F`、`gender:M`、`gender:여자`、`gender:남자`、`gender:여성`、`gender:남성`(すべて `여자`/`남자` にマップされます)
- `region:서울특별시`、`region:서울`(17の道、正式名称のエイリアスあり)
- `subregion:강남구`(`district` 列に対する接尾一致)
- `occupation_keyword:개발자`(部分文字列一致)

例は以下のとおりです。

```text
--filter "age:25-39,region:서울특별시"                    # 25-39 AND Seoul
--filter "age:25-39,region:서울특별시,region:경기도"      # 25-39 AND (Seoul OR Gyeonggi)
--filter "gender:F,occupation_keyword:디자이너"          # female AND occupation contains 디자이너
```

## Output Format

### Result JSON

インタビュー結果は `outputs/interview_{slug}_{YYYYMMDD_HHMMSS}.json` に書き出されます。エンベロープには実行メタデータ(`interview_id`、`slug`、`product`、`model`、`seed`、`config_snapshot`)と `records` 配列が含まれます。各レコードには `persona_meta`、マルチターンの `messages`、質問ごとの `raw_responses`、`structured_summary`、`flags` が格納されます。

| Field | Notes |
| --- | --- |
| `interview_id` | uuid、実行ごとに1つ |
| `schema_version` | v1.1.0以降は `2`(v1.0.x では `1` でした)。リーダーはこの値で `acceptable_price_signal` フィールドの処理を分岐できます |
| `model` | 解決済みのモデル id(例：`gpt-4o-mini`) |
| `meta_extra.usage` | 集計された `prompt_tokens`、`completion_tokens`、`total_tokens`、`cached_tokens` |
| `meta_extra.previous_run_id` | `--resume` から開始された実行で設定されます。元の実行の `interview_id` を保持します |
| `records[].status` | `completed` / `refused` / `failed` / `drift` |
| `records[].structured_summary` | `intent`、`acceptable_price_signal`、`willingness_to_pay`、`willingness_to_pay_currency`、`rejection_reasons`、`one_line` |
| `records[].flags` | `persona_drift`、`auto_follow_up_used`、`refusal_detected`、`truncated`、`parse_failed` |

完全なスキーマは `docs/prd/korea-persona-interview.md` の5.4節を参照してください。v1 の JSON ファイルは v1.1.0以降でも問題なく読み込めます(ローダーが `acceptable_price_signal=null` を埋めます)。

### Markdown report

report サブコマンドはデフォルトで入力 JSON の隣に `outputs/report_{slug}_{YYYYMMDD_HHMMSS}.md` を出力します。

```text
# 가상 인터뷰 리포트: {product}
| meta table | model, seed, persona counts, dataset, usage |

## 1. 정량 지표
### 1.1. 의향률          # intent share table + bar chart
### 1.2. 가격 수용가     # WTP median, IQR, histogram
### 1.3. 거절 사유 빈도  # top-N rejection reasons table
### 1.4. 코호트별 의향률 # age x region x gender, masked under min cell size

## 2. 정성 인사이트
### 2.1. 공통 반응       # up to 5 shared reactions
### 2.2. 인사이트        # 5-10 actionable insights
### 2.3. 코호트 차이     # cohort-level qualitative differences

## 3. 제외 record 요약   # excluded record counts and reasons

## 4. 한계와 출처        # synthetic-data caveat, dataset citation, model id
```

## Configuration

設定ポリシーは `시크릿은 환경 변수로、기본값은 yaml로、일회성 上書きは CLI로` です。設定の優先順位(後ろが前を上書きします)はビルトインデフォルト → `config.yaml` → CLI オプションです。

このツールが読み込む環境変数はシークレットと出力ディレクトリのみです。

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API キー(`provider=openai` の場合に使用) |
| `ANTHROPIC_API_KEY` | Anthropic API キー(`provider=anthropic` の場合に使用) |
| `KPI_OUTPUT_DIR` | 出力ディレクトリの上書き(テスト/CI 分離用に保持) |

注釈付きの完全な yaml は [config.yaml](../../../config.yaml) にあります。主なキーは以下のとおりです。

- `llm.provider` / `llm.base_url` / `llm.model` - プロバイダーとエンドポイント。デフォルトは `--provider anthropic` で切り替わります(`https://api.anthropic.com/v1` 上の `claude-haiku-4-5`)
- `llm.context_budget` - マルチターン履歴に対する32000トークンの予算(最も古い user/assistant ペアから削除され、システムプロンプトは保持されます)
- `llm.streaming` / `llm.anthropic_cache_control` / `llm.extra_chat_kwargs` - プロバイダー固有のチューニング
- `batch.concurrency`(1〜10、デフォルト4)と `batch.partial_failure_threshold`(デフォルト0.5)
- `common.dataset.field_map`、`common.dataset.gender_aliases`、`common.dataset.province_aliases` - データセットのスキーマ変更に備えた列/値のエイリアス
- `common.persona.fields` と `common.persona.system_prompt_path` - ペルソナトグルとシステムプロンプトテンプレートのパス
- `common.report.cohort_min_cell` / `histogram_bins` / `bar_width` / `insight_model` / `estimate_wtp_from_signal`
- `common.output.output_dir` / `log_level` / `no_color`
- `heuristics.short_answer_threshold` / `english_ratio_threshold` / `ambiguous_keywords` / `refusal_keywords` / `auto_follow_up_text` / `auto_follow_up_max` / `occupation_english_whitelist` / `llm_drift_review`
- `mcp.mode` - `orchestrator`(デフォルト、サーバーサイドキー不要)または `server`(サーバーサイドの OpenAI/Anthropic)。背景は ADR-005 を参照してください

### Choosing a model

`gpt-4o-mini` がデフォルトで、このワークロードに対して強力なベースラインを提供します。自身の実行でペルソナドリフト率が5%を超えて測定される場合は、以下の代替を試してください。

- `gpt-4o-mini`(OpenAI) - デフォルト。韓国語の流暢さとペルソナ遵守度に優れます
- `gpt-4o`(OpenAI) - より高い品質
- `claude-haiku-4-5`(Anthropic) - `--provider anthropic` のデフォルト
- `claude-sonnet-4-5` / `claude-opus-4-5`(Anthropic) - より高い品質
- `mlx_lm.server`、`vLLM`、`llama.cpp` 経由のローカル LLM は OpenAI Chat Completions API のサーフェスを公開していれば動作します。韓国語の流暢さは基盤の重みに依存するため、小さなサンプルで先にペルソナドリフトを検証してください

ペルソナドリフト動作は `gpt-4o-mini` でエンドツーエンドに検証済みです。他のモデルは閾値の調整が必要な場合があります(`heuristics.english_ratio_threshold`、`heuristics.short_answer_threshold`)。

### Customization

- システムプロンプトは `prompts/system_prompt.txt` で編集します(必ず `{persona_json}` と `{product}` のプレースホルダーを含める必要があります)。独自のテンプレートを使用するには `common.persona.system_prompt_path` を別のファイルに向けます
- ヒューリスティック閾値は `config.yaml` の `heuristics.*` で調整します(フォローアップを厳しくするには `short_answer_threshold` を下げ、技術ドメインでは `english_ratio_threshold` を上げ、`refusal_keywords`/`ambiguous_keywords` にドメイン固有の表現を追加します)
- レポート出力はマスキングを厳しくするために `common.report.cohort_min_cell` を5や7に上げ、狭いターミナル向けに `bar_width` を下げ、異なる価格解像度のために `histogram_bins` を調整します

## Integration with External Agents

エントリポイントは3種類あります。CLI、MCP サーバー、MCP オーケストレーターです。これらは互換ではありません - サーバーサイドの LLM 呼び出し(CLI、MCP サーバー)を望むのか、ホストエージェントのサブエージェントが LLM 処理を担当することを望むのか(MCP オーケストレーター)に応じて選択が変わります。

### Entry point matrix

| Entry point | mode (yaml) | Server-side LLM call | Host LLM call | API key required |
| --- | --- | --- | --- | --- |
| CLI(`kpi`) | n/a | yes | no | provider-dependent |
| MCP server | `mcp.mode: "server"` | yes | no | provider-dependent |
| MCP orchestrator | `mcp.mode: "orchestrator"`(default) | no | yes(host sub-agent) | none |

モード間の自動フォールバックはありません。選択された経路はすべての応答に `"backend": "mcp_server"` または `"backend": "mcp_orchestrator"` として反映されます。ADR-005 が背景を述べています(主流の MCP クライアントが capability を広告しなかったため、v1.2.0 で sampling モードが削除されました)。

`python -m src.mcp_server` を `mcp.mode: "orchestrator"` で MCP ホストの外で実行すると、ヘルパーツールは引き続き動作しますが、`interview` はブロックされ、代わりに `build_batch_prompts` + サブエージェント + `aggregate_results` を使うヒントが表示されます。

### Tool exposure by mode

| Tool | MCP server | MCP orchestrator | Notes |
| --- | --- | --- | --- |
| `healthcheck` | yes | yes | サーバーモードはプロバイダーに ping を送り、オーケストレーターモードは ok と cwd を返します |
| `list_personas` | yes | yes | フィルターに一致するペルソナをプレビューします |
| `interview` | yes | no(blocked) | サーバーサイドのバッチインタビュー |
| `report` | yes | yes | サーバーモードは定性インサイトの LLM 呼び出しを実行し、オーケストレーターモードはこれをスキップします |
| `build_persona_prompt` | no | yes | ペルソナ1人分のシステムプロンプトとペルソナ dict |
| `build_batch_prompts` | no | yes | N 人のペルソナ分のシステムプロンプト(ホストサブエージェントの fan-out) |
| `aggregate_results` | no | yes | ホストからレコードを受け取り、マークダウンレポートを生成します |
| `detect_persona_drift` / `should_auto_follow_up` / `parse_structured_summary` / `interview_record_schema` | yes | yes | ヘルパー。CLI と MCP サーバーは自動適用します。MCP オーケストレーターは明示的に呼び出す必要があります |

### Registering the MCP entry point

サーバーを手動で実行して起動を確認します。

```bash
python -m src.mcp_server
```

下のスニペットを `~/.claude/mcp.json` に追加して Claude Code に登録します(ファイルが存在しない場合は新規作成します)。`cwd` はプロジェクトルートを指す必要があります。これにより `config.yaml`、`prompts/system_prompt.txt`、`.env`、`outputs/` が正しく解決されます。

```json
{
  "mcpServers": {
    "korea-persona-interview": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/korea-persona-interview"
    }
  }
}
```

Cursor の場合はプロジェクトルートの `.cursor/mcp.json` にスニペットを追加します。ドロップイン用のコピーは [examples/mcp/](../../../examples/mcp/) にあります。

MCP サーバーモードでは初回実行前にプロジェクトの `.env` に `OPENAI_API_KEY`(または `ANTHROPIC_API_KEY`)を入れます。標準ライブラリの `.env` ローダーは `setdefault` のセマンティクスを採用しているため、シェルですでに export されたキーが優先されます。エージェントの mcp.json の `env` ブロックにキーを入れる方法も動作しますが、シークレットがエージェント設定にプレーンテキストで残り、git、dotfile 同期、スクリーンショットを通じて漏洩しやすくなります。

### MCP orchestrator mode usage (default)

ホストエージェントが LLM を所有します。流れは以下のとおりです。

1. `product`、`questions`、`n`(任意で `filter`、`seed`、`persona_ids`)で `build_batch_prompts` を呼び出します。N 個のシステムプロンプトとペルソナ dict が返ります
2. ホストは N 人のサブエージェント(ペルソナごとに1人)に fan-out します。各サブエージェントは自身の LLM に、返却されたシステムプロンプトをシステムメッセージとして、質問を user ターンとして渡します。ホストは CLI ヒューリスティックとの動作パリティを保つため、ターン間に `should_auto_follow_up` と `detect_persona_drift` を呼び出すこともできます
3. LLM 呼び出し後、ホストは LLM の構造化サマリーテキストに対して `parse_structured_summary` を呼び出し、正規化された dict を取得し、`interview_record_schema` に従ってレコードを組み立てます
4. ホストは組み立てた `records` で `aggregate_results` を呼び出します。このツールは定量集計を行い、マークダウンレポートを書き出します。定性インサイトはデフォルトでフォールバックメッセージで埋められますが、ホストは独自のものを `insights` として渡し本文に埋め込ませることができます

### MCP server mode usage

`config.yaml` で `mcp.mode: "server"` に設定すると、OpenAI/Anthropic をサーバーサイドで呼び出します。エージェントに普通の韓国語で "1인 가구 대상 반찬 정기배송(월 39,900원)을 25-39세 서울 30명에게 인터뷰 돌리고 리포트까지 만들어 줘" と依頼すると、`interview` の次に `report` を続けて呼び出し、マークダウンのパスを返します。

### --json mode for shell scripts

CLI を直接駆動するエージェントの場合、ルートグループに `--json` を渡します。tqdm、カラー、韓国語ラベルが無効化され、stdout に単一の JSON ドキュメントが出力されます。ログは引き続き stderr と `outputs/logs/run_*.jsonl` に流れます。

```bash
python main.py --json healthcheck
# {"ok": true, "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "models": [...]}

python main.py --json interview --product "..." --questions "..." --n 10
# {"ok": true, "output_path": "outputs/interview_*.json", "report_path": "outputs/report_*.md", "summary": {...}, "usage": {...}, "model": "gpt-4o-mini"}
```

エラーは `{"error": {"code": "...", "message": "...", "exit_code": N}}` の形式で0以外の終了コードとともに出力されます。

## Development

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip sync requirements.lock requirements-dev.lock
pytest tests/ -v
```

テストスイートは `pytest-httpx` で OpenAI/Anthropic API をモックし、monkeypatch fixture でデータセットをモックするため、実際の API キーやネットワークアクセスを必要としません。カバレッジは config、filter DSL、persona loader、LLM client/backend、インタビューセッション、ペルソナドリフト、バッチランナー、レポート定量、両モードの MCP dispatch、MCP オーケストレーターのヘルパーツール、エラーメッセージ、ロギング、CLI 統合に及びます。

実際の LLM API 呼び出しを行う手動 smoke テストは `tests/manual/` 以下にあり、デフォルト実行からは除外されています。

Conventional Commits を使用してください(`feat:`、`fix:`、`chore:`、`docs:`、`refactor:`、`test:`)。コミットに `Co-Authored-By` トレイラーを付けないでください。

## Limitations and Disclaimer

合成ペルソナは実際のユーザーインタビューの代替にはなりません。データセットは実際の回答者から標本抽出されたものではなく生成されたものなので、人口統計分布が実際の韓国の人口とは異なる場合があります。出力は実際の参加者を募集する前のクイックな直感チェック、そして募集予算を投じる前にインタビュー質問と製品コピーをプレッシャーテストする手段として扱ってください。

このツールが生成するすべてのレポートと JSON ファイルにも、フッターに合成データの免責事項が含まれます。

各インタビューに使用される `--product` テキストとペルソナメタデータは、設定した LLM エンドポイント(OpenAI、Anthropic、ローカルサーバー、または MCP ホストエージェントの LLM)に送信されます。未公開の IP、企業秘密、個人を特定できる情報を `--product` に入れないでください。ツールを実行する前に機微な部分を抽象化または言い換えてください。ツール自体は LLM 呼び出しと Hugging Face からの初回データセットダウンロード以外の外部テレメトリを送信しません。

API の請求はユーザーの責任です。トークン使用量(prompt / completion / cached)は各実行の最後に出力され、結果 JSON の `meta_extra.usage` に書き込まれ、レポートヘッダに表示されるため、プロバイダーの請求書と照合できます。ツールは USD コストの推定を行いません。ペルソナドリフトの品質は `gpt-4o-mini` に対して検証済みです。他のモデルは閾値の調整が必要な場合があります。

出力に対する法的/倫理的レビューはユーザーの責任です。ツールは入力シークレットポリシー以外のコンプライアンスや PII フィルターを実行しません。

## Roadmap

v1.3.0 候補の短いリストです。詳細は [docs/backlog/v1.3.0.md](../../backlog/v1.3.0.md) にあります。

- 同じアプリケーションレイヤー上の FastAPI REST API
- オフライン実行用の OpenAI Batch API パス
- マルチモデル A/B ルーティング(同じペルソナサンプルを2つの異なるモデルで実行し、出力を diff)
- プロバイダー品質検証レポート(OpenAI、Anthropic、ローカル LLM のゴールデンデータセットドリフト測定)
- API キー用の macOS Keychain / Linux libsecret / Windows Credential Manager 統合
- レコード単位のディスクストリーミング書き込み(バッチ途中の OOM/クラッシュで SIGINT 部分保存よりも失うレコードが少なくなるように)

## Dataset and Credits

このプロジェクトは [nvidia/Nemotron-Personas-Korea](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea) データセットを使用しています。

- Title: Nemotron-Personas-Korea
- Author: NVIDIA Corporation (2025)
- Source: https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea
- License: [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)
- Modifications: なし。データセットはランタイムに Hugging Face Hub からダウンロードされ、インメモリでサンプリングされます。このリポジトリはいかなる派生データセットも再配布しません

名前、性別、年齢、婚姻状況、学歴、職業、居住地(道と市郡区)、7つのペルソナファセット(professional、sports、arts、travel、culinary、family、summary)を扱う約100万レコードと700万人の韓国人合成ペルソナを含みます。

CC BY 4.0 は出典表示を条件として商用利用を許可しています。クレジットは NVIDIA Corporation に帰属します。このツールが生成するすべてのマークダウンレポートと JSON レコードは、フッターにデータセットの引用とライセンスを併せて含めるため、ダウンストリームの成果物にも出典が伝播します。

## Acknowledgments

このプロジェクトは [Claude Code](https://claude.com/claude-code) と共に開発されました。

## License

This project is licensed under the MIT License - see the [LICENSE](../../../LICENSE) file for details.
