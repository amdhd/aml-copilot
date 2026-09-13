# .env is loaded here so every target sees the same config, whichever
# terminal tab it runs in.
-include .env
export

CSV ?= data/HI-Small_Trans.csv
# uv from PATH where it is available; the explicit path is the fallback for
# shells that do not have it, and is wrong anywhere else (CI, containers).
UV  := PYTHONUNBUFFERED=1 $(shell command -v uv || echo $(HOME)/.local/bin/uv)

train:
	$(UV) run python -m ml.train --csv $(CSV) $(ARGS)

baseline:
	$(UV) run python -m ml.baseline --csv $(CSV) $(ARGS)

score:
	$(UV) run python -m ml.score_batch --csv $(CSV) $(ARGS)

api:
	$(UV) run uvicorn api.main:app --port 8000

worker:
	$(UV) run arq api.worker.WorkerSettings

ask:
	$(UV) run python -m scripts.ask $(MSG)

test:
	$(UV) run pytest

eval:
	$(UV) run python -m eval.run_evals

tensorboard:
	$(UV) run tensorboard --logdir runs --port 6006

smoke:
	$(UV) run python scripts/make_smoke_csv.py
	$(MAKE) train CSV=data/smoke_Trans.csv ARGS="--epochs 5"
	$(MAKE) baseline CSV=data/smoke_Trans.csv

.PHONY: train baseline score api worker ask test eval tensorboard smoke
