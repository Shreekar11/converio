
.PHONY: generate-schemas install test lint format build

install:
	uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check app/
	.venv/bin/mypy app/

format:
	.venv/bin/black app/ tests/

generate-schemas:
	.venv/bin/python -m datamodel_code_generator --input app/api/v1/specs/auth.json --output app/schemas/generated/auth.py --output-model-type pydantic_v2.BaseModel
	.venv/bin/python -m datamodel_code_generator --input app/api/v1/specs/candidates.json --output app/schemas/generated/candidates.py --output-model-type pydantic_v2.BaseModel
	.venv/bin/python -m datamodel_code_generator --input app/api/v1/specs/companies.json --output app/schemas/generated/companies.py --output-model-type pydantic_v2.BaseModel
	.venv/bin/python -m datamodel_code_generator --input app/api/v1/specs/jobs.json --output app/schemas/generated/jobs.py --output-model-type pydantic_v2.BaseModel
	.venv/bin/python -m datamodel_code_generator --input app/api/v1/specs/recruiters.json --output app/schemas/generated/recruiters.py --output-model-type pydantic_v2.BaseModel

build:
	docker build -t converio-backend --build-arg BUILD_MODE=development .
