SCRIPT := skills/chrome-profiles/scripts/chrome-browser-map.py

.PHONY: test validate

test:
	python3 $(SCRIPT) --self-check

validate:
	jq empty .claude-plugin/plugin.json .claude-plugin/marketplace.json
	claude plugin validate .
