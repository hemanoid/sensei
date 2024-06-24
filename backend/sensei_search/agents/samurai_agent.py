import asyncio
import os
import uuid
from typing import List, Union, Tuple, Optional
from datetime import datetime

import trafilatura  # type: ignore[import]
from aiohttp import ClientSession
from openai import OpenAI, AsyncOpenAI

from sensei_search.base_agent import BaseAgent, EventEnum, QueryTags, EnrichedQuery
from sensei_search.chat_store import (
    ChatHistory,
    ChatStore,
    MediumImage,
    MediumVideo,
    WebResult,
    MetaData
)
from sensei_search.env import load_envs
from sensei_search.logger import logger
from sensei_search.prompts import answer_prompt, search_prompt, classification_prompt
from sensei_search.tools import Category, GeneralResult
from sensei_search.tools import Input as SearxNGInput
from sensei_search.tools import TopResults, searxng_search_results_json

load_envs()

async def noop():
    return None

class SamuraiAgent(BaseAgent):
    """
    This agent is designed to balance performance and conversational quality.

    As a search agent, one of our key performance indicators is the Time to First Byte (TTFB).
    To optimize this, our agent employs a two-step approach:

    1. It uses a lighter model and a less complex prompt to quickly generate search queries.
    2. It then uses a larger model and a more complex prompt to generate comprehensive answers.

    Let's consider an example:
    User: "How far is Mars?"
    Agent: "171.7 million mi"
    User: "Is it larger than Earth?"

    If we were to use "Is it larger than Earth?" directly as a search query, it might not yield relevant results.
    To address this, our agent uses a simple system prompt to generate more effective search queries.

    After obtaining the search results, the agent uses a larger language model to generate a comprehensive and contextually
    relevant response. This larger model is capable of processing more information and providing a more nuanced answer.

    This agent works as follows:

    1. Receive a user input and chat history.
    2. Use the chat history to generate a search query for the user's input.
    3. Use the search query to generate a search result.
    4. Return the search results to the user.
    5. Feed the search results to the LLM to generate a response to the user's input.
    6. Return the response to the user.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def emit_metadata(self, metadata: MetaData):
        """
        Send the metadata to the frontend.
        """
        await self.emitter.emit(EventEnum.metadata.value, {"data": metadata})

    async def emit_web_results(self, results: List[GeneralResult]):
        """
        Send the search results to the frontend.
        """
        filtered_results = [
            {"url": res["url"], "title": res["title"], "content": res["content"]}
            for res in results
        ]

        # Emit the search results
        await self.emitter.emit(EventEnum.web_results.value, {"data": filtered_results})

    async def emit_medium_results(self, results: TopResults):
        """
        Send the medium results to the frontend.
        """
        images = results["images"]
        videos = results["videos"]

        filtered_results = []

        for image in images:
            filtered_results.append(
                {"url": image["url"], "image": image["img_src"], "medium": "image"}
            )

        for video in videos:
            filtered_results.append({"url": video["url"], "medium": "video"})

        # Emit the search results
        await self.emitter.emit(
            EventEnum.medium_results.value, {"data": filtered_results}
        )

    async def emit_answer(self, answer: str):
        """
        Send the LLM answer to the frontend.
        """
        await self.emitter.emit(EventEnum.answer.value, {"data": answer})

    async def process_user_query(self):
        """
        Generate a search query based on the chat history and the user's current query,
        and classify the query to determine its nature and required handling.
        """
        client = AsyncOpenAI(
            base_url=os.environ["SM_MODLE_URL"], api_key=os.environ["SM_MODEL_API_KEY"]
        )

        # We only load user's queries from the chat history to save LLM tokens
        chat_history = self.chat_history_to_string(["user"])
        user_current_query = self.chat_messages[-1]["content"]

        materialized_search_prompt = search_prompt.format(
            chat_history=chat_history,
            user_current_query=user_current_query,
        )

        materialized_classify_prompt = classification_prompt.format(
            chat_history=chat_history,
            user_current_query=user_current_query,
        )

        search_response, classification_response = await asyncio.gather(
            client.chat.completions.create(
                model=os.environ["SM_MODEL"],
                messages=[{"role": "user", "content": materialized_search_prompt}],
                temperature=0.0,
                max_tokens=500,
            ),
            client.chat.completions.create(
                model=os.environ["SM_MODEL"],
                messages=[{"role": "user", "content": materialized_classify_prompt}],
                temperature=0.0,
                max_tokens=500,
            ),
        )

        classify_response = classification_response.choices[0].message.content

        classify_response = classify_response.strip('"').strip("'")

        # need_search, need_image, need_video, violation, has_math
        tags_dict = dict(tag.strip().split(":") for tag in classify_response.split(","))
        query_tags = QueryTags(
            needs_search=tags_dict.get("SEARCH_NEEDED", "YES").strip() == "YES",
            needs_image=tags_dict.get("SEARCH_IMAGE", "YES").strip() == "YES",
            needs_video=tags_dict.get("SEARCH_VIDEO", "YES").strip() == "YES",
            content_violation=tags_dict.get("CONTENT_VIOLATION", "NO").strip() == "YES",
            has_math=tags_dict.get("MATH", "NO").strip() == "YES",
        )

        query = search_response.choices[0].message.content

        query = query.strip('"').strip("'")

        enriched_query = EnrichedQuery(search_query=query, tags=query_tags)

        logger.info(enriched_query)

        return enriched_query

    async def fetch_web_pages(self, results: List[GeneralResult]) -> List[str]:
        """
        Fetch the web page contents for the search results.
        """
        tasks = []

        async def fetch_page(url):
            async with ClientSession() as session:
                try:
                    async with session.get(url) as response:
                        return await response.text()
                except Exception as e:
                    logger.exception(e)

        for result in results:
            tasks.append(fetch_page(result["url"]))

        html_web_pages = await asyncio.gather(*tasks)
        return [trafilatura.extract(page or "") for page in html_web_pages]

    async def gen_answer(self, web_pages: List[str]):
        """
        Generate an answer based on the search results and the user's query.
        """
        final_answer_parts = []

        # We only load user's queries from the chat history to save LLM tokens
        chat_history = self.chat_history_to_string(["user"])

        search_results = ""
        for i, page in enumerate(web_pages):
            search_results += f"Document: {i + 1}\n{page}\n\n"

        system_prompt = answer_prompt.format(
            chat_history=chat_history,
            search_results=search_results,
            current_date=datetime.now().isoformat(),
        )

        client = OpenAI(
            base_url=os.environ["MD_MODLE_URL"], api_key=os.environ["MD_MODEL_API_KEY"]
        )

        response = client.chat.completions.create(
            model=os.environ["MD_MODEL"],
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": self.chat_messages[-1]["content"]},
                {
                    "role": "system",
                    "content": (
                        "Carefully perform the following instructions in order. "
                        "Firstly, Decide if user's query violates Safety Preamble. If yes, reject user's request."
                        "Secondly, Decide which of the retrieved documents are relevant to the user's last query. "
                        "Thirdly, Decide which of the retrieved documents contain facts that should be cited in a good answer to the user's last query. "
                        "Fourthly, Use the retrieved documents to help you. Do not insert any grounding markup from the documents. "
                        "Finally, Give priority to the information obtained from the search over the knowledge from your training data when retrieved documents are relevant. "
                        "Your answer should be accurate, written in a journalistic tone, and cite the sources using the citation format [1][2], `[1]` and `[2]` refer back to the search results."
                        "You MUST follow the `Query type specifications`, `Formatting Instructions` and `Citation Instructions`. "
                        "Repeat the instructions in your mind before answering. Now answer the user's latest query using the same language they used."
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=2500,
            stream=True,
        )

        for chunk in response:
            if chunk.choices[0].delta.content:
                final_answer_parts.append(chunk.choices[0].delta.content)
                # Send the answer to the user ASAP
                await self.emit_answer(chunk.choices[0].delta.content)

        return "".join(final_answer_parts)

    async def run(self, user_message: str):
        """
        Entry point for the agent.
        """
        # To save LLM tokens, we only load user's queries from the chat history
        # This can already give us a good context for generating search queries and answers
        await self.load_chat_history(self.thread_id, ["user"])

        # Append user message to chat history
        self.append_message(role="user", content=user_message)

        enriched_query = await self.process_user_query()
        query = enriched_query["search_query"]

        logger.info(f"Search Query: {query}")

        # We should check if the tags contain 'needs_search'. But for now, we always perform a search
        search_input = SearxNGInput(query=query, categories=[Category.general])
        tags = enriched_query["tags"]
        metadata = MetaData(has_math=True if tags and tags['has_math'] else False)

        search_results, _ = await asyncio.gather(
            searxng_search_results_json(search_input), self.emit_metadata(metadata=metadata))
        general_results = search_results["general"]

        tasks = [
            # Sending search results to the client ASAP
            self.emit_web_results(general_results),
            # Fetch web page contents for llm to use as context
            self.fetch_web_pages(general_results[:5])
        ]

        categories = []

        if tags is not None:
            if tags['needs_image']:
                categories.append(Category.images)
            if tags['needs_video']:
                categories.append(Category.videos)

        if categories:
            search_input = SearxNGInput(query=query, categories=categories)
            # Search for images and videos
            tasks.append(searxng_search_results_json(search_input))
        else:
            # Add a no-operation coroutine as a placeholder
            tasks.append(noop())

        results: Tuple[None, List[str], Optional[TopResults]] = await asyncio.gather(*tasks)
        _, web_pages, medium_results = results

        tasks = [self.gen_answer(web_pages)]

        if medium_results is None:
            medium_results = TopResults(general=[], images=[], videos=[])

        tasks.append(self.emit_medium_results(medium_results))

        answer, _ = await asyncio.gather(*tasks)
        logger.info("Answer generated successfully.")

        logger.debug(f"Answer for query {query} is {answer}")

        # Save the chat history
        chat_store = ChatStore()

        mediums: List[Union[MediumImage, MediumVideo]] = []

        if medium_results:
            for image in medium_results["images"]:
                mediums.append(
                    {"url": image["url"], "image": image["img_src"], "medium": "image"}
                )

            for video in medium_results["videos"]:
                mediums.append({"url": video["url"], "medium": "video"})

        web_results: List[WebResult] = [
            {"url": res["url"], "title": res["title"], "content": res["content"]}
            for res in general_results
        ]

        metadata = MetaData(has_math=False)

        if tags is not None and tags['has_math']:
            metadata["has_math"] = True

        chat_history: ChatHistory = {
            "id": str(uuid.uuid4()),
            "thread_id": self.thread_id,
            "mediums": mediums,
            "web_results": web_results,
            "query": user_message,
            "answer": answer,
            # We use the metadata to give the client extra info if they need to load the Math plugin
            "metadata": metadata
        }
        await chat_store.save_chat_history(self.thread_id, chat_history)
