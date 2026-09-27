## Cost versus accuracy on `validation`

| configuration | top 1 | top 3 | median USD | mean tool calls | tokens |
|---|---|---|---|---|---|
| all-strong (`a3`) | 0.143 | 0.571 | not measured | 1.2 | 0 |
| all-small (`a4`) | 0.143 | 0.571 | not measured | 1.2 | 0 |
| cascade (`fb-v1`) | 0.143 | 0.571 | not measured | 1.2 | 0 |

**No cost was measured.** Every run above used no model: a stub completion reports zero tokens and zero dollars on purpose, because inventing a token count would put fiction into the cost column of every run CI makes. The accuracy columns are real and the cost column is absent rather than zero, since three zeros would read as "the cascade is free".

To fill it in: configure a tier in `config/models.yaml` with a sourced price, set `LLM_BASE_URL` and `LLM_API_KEY`, and run this again.

No model in `config/models.yaml` carries a price, so no cost could be computed even from real token counts.
