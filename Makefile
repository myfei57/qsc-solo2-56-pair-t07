test:
	PYTHONPATH=. python -m pytest -q

# 离线环境没有 pytest 时的零依赖兜底（同一套 tests/，语义以 pytest 为准）
test-offline:
	PYTHONPATH=. python tools/run_tests.py

run:
	PYTHONPATH=. python -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080

# 同时挂载应急演练服务（现场接口与演练接口隔离，动作不外发）
run-drill:
	PYTHONPATH=. python -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080 --with-drill

demo:
	PYTHONPATH=. python -m psdline --config config/psdline.json demo --fast

# 应急演练端到端自演示：两种模式、跨进程崩溃恢复、隔离校验、篡改检测
drill-demo:
	PYTHONPATH=. python tools/drill_demo.py

deadcode:
	PYTHONPATH=. python tools/deadcode.py psdline
