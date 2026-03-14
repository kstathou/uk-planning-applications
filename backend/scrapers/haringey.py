import asyncio

from backend.prompts.haringey import TASK
from backend.scrapers.base import create_agent


async def scrape_haringey(
    vllm_endpoint: str,
    vllm_api_key: str,
    vllm_model: str,
    output_dir: str = "data",
) -> None:
    """Scrape planning applications from Haringey Council."""
    agent = create_agent(
        task=TASK,
        vllm_endpoint=vllm_endpoint,
        vllm_api_key=vllm_api_key,
        vllm_model=vllm_model,
        output_dir=output_dir,
        headless=True,
    )
    history = await agent.run(max_steps=100)
    print(f"Haringey scrape complete. Steps: {history.number_of_steps()}")
    if history.has_errors():
        print(f"Errors: {history.errors()}")


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(
        scrape_haringey(
            vllm_endpoint=os.environ["VLLM_ENDPOINT"],
            vllm_api_key=os.environ["VLLM_API_KEY"],
            vllm_model=os.environ["VLLM_MODEL"],
            output_dir="data",
        )
    )
