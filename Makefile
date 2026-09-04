CSV ?= data/HI-Small_Trans.csv
UV  := PYTHONUNBUFFERED=1 $(HOME)/.local/bin/uv

train:
	$(UV) run python -m ml.train --csv $(CSV) $(ARGS)

baseline:
	$(UV) run python -m ml.baseline --csv $(CSV) $(ARGS)

score:
	$(UV) run python -m ml.score_batch --csv $(CSV) $(ARGS)

tensorboard:
	$(UV) run tensorboard --logdir runs --port 6006

smoke:
	$(UV) run python scripts/make_smoke_csv.py
	$(MAKE) train CSV=data/smoke_Trans.csv ARGS="--epochs 5"
	$(MAKE) baseline CSV=data/smoke_Trans.csv

.PHONY: train baseline score tensorboard smoke
