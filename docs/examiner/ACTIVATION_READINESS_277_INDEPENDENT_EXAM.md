# Independent examination of activation-readiness PR #277

**Examiner:** Cursor (this branch). Claude built the candidate and cannot certify it.
**Verdict:** pending local reproduction (filled in after pytest and GitHub product-CI inspection).
**Candidate:** `claude/mainai-activation-readiness-batch` @ `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc`
**Composition parent:** `claude/activation-base-276-263` @ `93696e22e9d75728a3f9c34a1cd971e14d37f195`

Do not amend #277, #276, #263, or Codex activation-security work. This branch adds examiner evidence only.

---

## Frozen SHAs verified

| Object | SHA | Result |
|---|---|---|
| #277 head (remote) | `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc` | Matches claimed freeze |
| #277 parent | `93696e22e9d75728a3f9c34a1cd971e14d37f195` | Merge of `6c75174` + `e33f494` |
| #276 frozen tip | `09b87e9b70dc02889d39c2bae3d56458509d49cb` | **Not** an ancestor of the composition parent |
| #276 implementation (TA-8b/TA-10) | `29d230acd1facc469db3d34844c54120c907b71c` | Is an ancestor |
| #276 demo_1 fixture | `dc675247744169d6fa4650f9be4cf1d972aa3792` | **Not** an ancestor |
| #263 freeze | `e33f494c4fc53704c494cecea788f3e22be36330` | Is an ancestor |

---

## Scorecard (filled during exam)

See the final section after local tests land.
