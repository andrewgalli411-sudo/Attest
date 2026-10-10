.PHONY: backend frontend test fixture
backend:
	cd backend && uv run uvicorn auditor.main:create_app --factory --reload --port 8000
frontend:
	cd frontend && pnpm dev
test:
	cd backend && uv run pytest -q
fixture:
	cd backend/fixtures/site && python3 -m http.server 8100
