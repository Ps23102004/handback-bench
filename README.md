# handback-bench

Code and transcripts for "Finished, but Told to Keep Going: how a local Gemma 4 agent responds to a false 'not done' signal".

- `handback_pilot.py`: the harness. Python standard library only. It needs a local Ollama server to run a model, and nothing at all to re-score saved transcripts.
- `results_*_1794436202.jsonl`: Run A (base, nudge, guard; 96 episodes).
- `results_*_ctx4096_1794443258.jsonl`: context-window probe (8 episodes).
- `results_*_1794446125.jsonl`: Run B (reprompt, pressure; 64 episodes).
- `results_*_1794443736.jsonl`: aborted first attempt at Run B (one episode, before the reply cap was added).

Each line of a results file is one episode with its full message transcript.

## Reproduce the tables without a model

```bash
python3 handback_pilot.py --selftest
python3 handback_pilot.py --audit results_gemma4-12b-mlx_oh40_ctx32768_1794436202.jsonl
python3 handback_pilot.py --audit results_gemma4-12b-mlx_oh40_ctx32768_1794446125.jsonl
```

## Run it yourself

```bash
python3 handback_pilot.py --model gemma4:12b-mlx --overhead 40 --num-ctx 32768
python3 handback_pilot.py --model gemma4:12b-mlx --overhead 40 --num-ctx 32768 --conds reprompt,pressure
```

The agent has five tools (list, read, write, ask the user, finish) and cannot execute code. `--overhead 40` adds 40 tools the tasks never need. `--inspect <results file> --family <name>` prints what the agent wrote and said.

## Known issues

- The first scoring of Run A used a TODO-task checker that rejected `src/`-prefixed file names. The checker now accepts both forms; `--audit` re-scores old files.
- Run A was recorded before the per-reply token cap and the timeout handling were added.
- `--num-ctx` was not enforced by the Ollama runtime used here.
