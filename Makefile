.PHONY: help test eval demo gate clean

help:
	@echo "make test   component checks (99)"
	@echo "make eval   benchmark + vacuity ablation + report"
	@echo "make demo   scripted walkthrough"
	@echo "make gate   everything the pre-deploy gate runs"

test:
	python scripts/self_test.py

eval:
	python eval/run_eval.py --ablate --report

demo:
	python run.py demo

gate: test eval
	python scripts/preflight.py

clean:
	rm -rf data logs __pycache__ */__pycache__
