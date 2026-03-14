from browser_use import Agent, Browser, ChatOpenAI

from backend.tools.save import create_save_tools


def create_agent(
    task: str,
    vllm_endpoint: str,
    vllm_api_key: str,
    vllm_model: str,
    output_dir: str = "data",
    headless: bool = True,
) -> Agent:
    """Create a browser-use agent configured for planning portal scraping."""
    llm = ChatOpenAI(
        model=vllm_model,
        base_url=vllm_endpoint,
        api_key=vllm_api_key,
    )

    browser = Browser(headless=headless)

    tools = create_save_tools(output_dir=output_dir)

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        tools=tools,
        use_vision=True,
        max_actions_per_step=3,
        generate_gif=False,
    )

    return agent
