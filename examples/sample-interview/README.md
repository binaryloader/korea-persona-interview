# Sample Interview Output

A real run captured against `gpt-4o-mini` so you can preview the artifact shapes before installing the project.

## Scenario

| Field | Value |
| --- | --- |
| Product idea | AI English conversation coach app, KRW 19,900/month, daily 15-minute 1:1 video sessions |
| Personas | 10, sampled from `nvidia/Nemotron-Personas-Korea` |
| Filter | `age:25-44,region:서울특별시` |
| Seed | `100` |
| Provider / Model | OpenAI / `gpt-4o-mini` |
| Concurrency | 5 |
| Wall time | 27.8 s (about 2.8 s per persona) |
| Token usage | prompt 48,765 / completion 3,780 / cached 26,240 (54% cache hit) |
| Cost estimate | $0.0076 |
| Status breakdown | 10 completed, 0 drift, 0 refused, 0 failed |

## Files

- `sample-interview.json` - Raw interview record array. Schema is documented in [docs/prd/korea-persona-interview.md](../../docs/prd/korea-persona-interview.md) section 5.4
- `sample-report.md` - Auto-generated markdown report (quantitative metrics, qualitative insights, exclusion summary, dataset attribution)

## Reproduce

```bash
python main.py interview --product "AI 영어 회화 코치 앱, 월 19,900원, 매일 15분 1:1 화상 대화" --filter "age:25-44,region:서울특별시" --n 10 --seed 100 --concurrency 5 --questions "이 서비스에 가입하실 의향이 있으세요?" --questions "월 19,900원이라는 가격을 어떻게 생각하세요?" --questions "거절하신다면 어떤 이유 때문일까요?" --questions "어떤 기능이 추가되면 가입하시겠어요?"
```

The same seed plus the same filter plus the same dataset version returns the same 10 personas. Model output is non-deterministic by default (`temperature=0.8`) so individual answers will vary, but the persona identities and quantitative shape stay stable.

## Disclaimer

Synthetic personas do not replace real user research. Treat this artifact as a hypothesis stress test, not as evidence. See the project [README](../../README.md) Limitations section for the full disclaimer.
