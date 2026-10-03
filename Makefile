test:
	PYTHONPATH=. python3 -m pytest -q

run:
	PYTHONPATH=. python3 -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080

demo:
	PYTHONPATH=. python3 -m psdline --config config/psdline.json demo --fast

# 应急演练自检：用临时目录完整跑一遍“捕获快照 → 开演 → 处置 → 复盘 → 校验”，
# 全程不触碰默认现场存储（var/state）。
drill-demo:
	PYTHONPATH=. python3 tools/drill_demo.py

deadcode:
	PYTHONPATH=. python3 tools/deadcode.py psdline
