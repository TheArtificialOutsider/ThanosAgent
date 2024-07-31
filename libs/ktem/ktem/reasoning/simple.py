import asyncio
import html
import logging
import re
from collections import defaultdict
from difflib import SequenceMatcher
from functools import partial
from typing import Generator

import numpy as np
import tiktoken
from ktem.llms.manager import llms
from ktem.reasoning.prompt_optimization import (
    DecomposeQuestionPipeline,
    RewriteQuestionPipeline,
)
from ktem.utils.render import Render
from theflow.settings import settings as flowsettings

from kotaemon.base import (
    AIMessage,
    BaseComponent,
    Document,
    HumanMessage,
    Node,
    RetrievedDocument,
    SystemMessage,
)
from kotaemon.indices.qa.citation import CitationPipeline
from kotaemon.indices.splitters import TokenSplitter
from kotaemon.llms import ChatLLM, PromptTemplate
from kotaemon.loaders.utils.gpt4v import generate_gpt4v, stream_gpt4v

from .base import BaseReasoning

logger = logging.getLogger(__name__)

EVIDENCE_MODE_TEXT = 0
EVIDENCE_MODE_TABLE = 1
EVIDENCE_MODE_CHATBOT = 2
EVIDENCE_MODE_FIGURE = 3


def get_header(doc: Document):
    """Get the header for the document"""
    header = ""
    if "page_label" in doc.metadata:
        header += f" [Page {doc.metadata['page_label']}]"

    header += f" {doc.metadata.get('file_name', '<Unknown>')}"
    return header.strip()


def is_close(val1, val2, tolerance=1e-9):
    return abs(val1 - val2) <= tolerance


def find_text(search_span, context):
    sentence_list = search_span.split("\n")
    matches = []
    # don't search for small text
    if len(search_span) > 5:
        for sentence in sentence_list:
            match = SequenceMatcher(
                None, sentence, context, autojunk=False
            ).find_longest_match()
            if match.size > len(sentence) * 0.35:
                matches.append((match.b, match.b + match.size))

    return matches


_default_token_func = tiktoken.encoding_for_model("gpt-3.5-turbo").encode


class PrepareEvidencePipeline(BaseComponent):
    """Prepare the evidence text from the list of retrieved documents

    This step usually happens after `DocumentRetrievalPipeline`.

    Args:
        trim_func: a callback function or a BaseComponent, that splits a large
            chunk of text into smaller ones. The first one will be retained.
    """

    trim_func: TokenSplitter = TokenSplitter.withx(
        chunk_size=32000,
        chunk_overlap=0,
        separator=" ",
        tokenizer=partial(
            tiktoken.encoding_for_model("gpt-3.5-turbo").encode,
            allowed_special=set(),
            disallowed_special="all",
        ),
    )

    def run(self, docs: list[RetrievedDocument]) -> Document:
        evidence = ""
        table_found = 0
        evidence_modes = []

        for _id, retrieved_item in enumerate(docs):
            retrieved_content = ""
            page = retrieved_item.metadata.get("page_label", None)
            source = filename = retrieved_item.metadata.get("file_name", "-")
            if page:
                source += f" (Page {page})"
            if retrieved_item.metadata.get("type", "") == "table":
                evidence_modes.append(EVIDENCE_MODE_TABLE)
                if table_found < 5:
                    retrieved_content = retrieved_item.metadata.get("table_origin", "")
                    if retrieved_content not in evidence:
                        table_found += 1
                        evidence += (
                            f"<br><b>Table from {source}</b>\n"
                            + retrieved_content
                            + "\n<br>"
                        )
            elif retrieved_item.metadata.get("type", "") == "chatbot":
                evidence_modes.append(EVIDENCE_MODE_CHATBOT)
                retrieved_content = retrieved_item.metadata["window"]
                evidence += (
                    f"<br><b>Chatbot scenario from {filename} (Row {page})</b>\n"
                    + retrieved_content
                    + "\n<br>"
                )
            elif retrieved_item.metadata.get("type", "") == "image":
                evidence_modes.append(EVIDENCE_MODE_FIGURE)
                retrieved_content = retrieved_item.metadata.get("image_origin", "")
                retrieved_caption = html.escape(retrieved_item.get_content())
                evidence += (
                    f"<br><b>Figure from {source}</b>\n"
                    + f"<img width='85%' src='{retrieved_content}' "
                    + f"alt='{retrieved_caption}'/>"
                    + "\n<br>"
                )
            else:
                if "window" in retrieved_item.metadata:
                    retrieved_content = retrieved_item.metadata["window"]
                else:
                    retrieved_content = retrieved_item.text
                retrieved_content = retrieved_content.replace("\n", " ")
                if retrieved_content not in evidence:
                    evidence += (
                        f"<br><b>Content from {source}: </b> "
                        + retrieved_content
                        + " \n<br>"
                    )

        # resolve evidence mode
        evidence_mode = EVIDENCE_MODE_TEXT
        if EVIDENCE_MODE_FIGURE in evidence_modes:
            evidence_mode = EVIDENCE_MODE_FIGURE
        elif EVIDENCE_MODE_TABLE in evidence_modes:
            evidence_mode = EVIDENCE_MODE_TABLE

        if evidence_mode != EVIDENCE_MODE_FIGURE:
            # trim context by trim_len
            print("len (original)", len(evidence))
            if evidence:
                texts = self.trim_func([Document(text=evidence)])
                evidence = texts[0].text
                print("len (trimmed)", len(evidence))

        return Document(content=(evidence_mode, evidence))


DEFAULT_QA_TEXT_PROMPT = (
    "Use the following pieces of context to answer the question at the end in detail with clear explanation. "  # noqa: E501
    "If you don't know the answer, just say that you don't know, don't try to "
    "make up an answer. Give answer in "
    "{lang}.\n\n"
    "{context}\n"
    "Question: {question}\n"
    "Helpful Answer:"
)

DEFAULT_QA_TABLE_PROMPT = (
    "Use the given context: texts, tables, and figures below to answer the question, "
    "then provide answer with clear explanation."
    "If you don't know the answer, just say that you don't know, "
    "don't try to make up an answer. Give answer in {lang}.\n\n"
    "Context:\n"
    "{context}\n"
    "Question: {question}\n"
    "Helpful Answer:"
)  # noqa

DEFAULT_QA_CHATBOT_PROMPT = (
    "Pick the most suitable chatbot scenarios to answer the question at the end, "
    "output the provided answer text. If you don't know the answer, "
    "just say that you don't know. Keep the answer as concise as possible. "
    "Give answer in {lang}.\n\n"
    "Context:\n"
    "{context}\n"
    "Question: {question}\n"
    "Answer:"
)  # noqa

DEFAULT_QA_FIGURE_PROMPT = (
    "Use the given context: texts, tables, and figures below to answer the question. "
    "If you don't know the answer, just say that you don't know. "
    "Give answer in {lang}.\n\n"
    "Context: \n"
    "{context}\n"
    "Question: {question}\n"
    "Answer: "
)  # noqa

DEFAULT_REWRITE_PROMPT = (
    "Given the following question, rephrase and expand it "
    "to help you do better answering. Maintain all information "
    "in the original question. Keep the question as concise as possible. "
    "Give answer in {lang}\n"
    "Original question: {question}\n"
    "Rephrased question: "
)  # noqa

CONTEXT_RELEVANT_WARNING_SCORE = 0.7


class AnswerWithContextPipeline(BaseComponent):
    """Answer the question based on the evidence

    Args:
        llm: the language model to generate the answer
        citation_pipeline: generates citation from the evidence
        qa_template: the prompt template for LLM to generate answer (refer to
            evidence_mode)
        qa_table_template: the prompt template for LLM to generate answer for table
            (refer to evidence_mode)
        qa_chatbot_template: the prompt template for LLM to generate answer for
            pre-made scenarios (refer to evidence_mode)
        lang: the language of the answer. Currently support English and Japanese
    """

    llm: ChatLLM = Node(default_callback=lambda _: llms.get_default())
    vlm_endpoint: str = getattr(flowsettings, "KH_VLM_ENDPOINT", "")
    citation_pipeline: CitationPipeline = Node(
        default_callback=lambda _: CitationPipeline(llm=llms.get_default())
    )

    qa_template: str = DEFAULT_QA_TEXT_PROMPT
    qa_table_template: str = DEFAULT_QA_TABLE_PROMPT
    qa_chatbot_template: str = DEFAULT_QA_CHATBOT_PROMPT
    qa_figure_template: str = DEFAULT_QA_FIGURE_PROMPT

    enable_citation: bool = False
    system_prompt: str = ""
    lang: str = "English"  # support English and Japanese
    n_last_interactions: int = 5

    def get_prompt(self, question, evidence, evidence_mode: int):
        """Prepare the prompt and other information for LLM"""
        images = []

        if evidence_mode == EVIDENCE_MODE_TEXT:
            prompt_template = PromptTemplate(self.qa_template)
        elif evidence_mode == EVIDENCE_MODE_TABLE:
            prompt_template = PromptTemplate(self.qa_table_template)
        elif evidence_mode == EVIDENCE_MODE_FIGURE:
            prompt_template = PromptTemplate(self.qa_figure_template)
        else:
            prompt_template = PromptTemplate(self.qa_chatbot_template)

        if evidence_mode == EVIDENCE_MODE_FIGURE:
            # isolate image from evidence
            evidence, images = self.extract_evidence_images(evidence)
            prompt = prompt_template.populate(
                context=evidence,
                question=question,
                lang=self.lang,
            )
        else:
            prompt = prompt_template.populate(
                context=evidence,
                question=question,
                lang=self.lang,
            )

        return prompt, evidence, images

    def run(
        self, question: str, evidence: str, evidence_mode: int = 0, **kwargs
    ) -> Document:
        return self.invoke(question, evidence, evidence_mode, **kwargs)

    def invoke(
        self, question: str, evidence: str, evidence_mode: int = 0, **kwargs
    ) -> Document:
        history = kwargs.get("history", [])
        prompt, evidence, images = self.get_prompt(question, evidence, evidence_mode)

        output = ""
        if evidence_mode == EVIDENCE_MODE_FIGURE:
            output = generate_gpt4v(self.vlm_endpoint, images, prompt, max_tokens=768)
        else:
            messages = []
            if self.system_prompt:
                messages.append(SystemMessage(content=self.system_prompt))
            for human, ai in history[-self.n_last_interactions :]:
                messages.append(HumanMessage(content=human))
                messages.append(AIMessage(content=ai))
            messages.append(HumanMessage(content=prompt))
            output = self.llm(messages).text

        # retrieve the citation
        citation = None
        if evidence and self.enable_citation:
            citation = self.citation_pipeline.invoke(
                context=evidence, question=question
            )

        answer = Document(text=output, metadata={"citation": citation})

        return answer

    async def ainvoke(  # type: ignore
        self, question: str, evidence: str, evidence_mode: int = 0, **kwargs
    ) -> Document:
        """Answer the question based on the evidence

        In addition to the question and the evidence, this method also take into
        account evidence_mode. The evidence_mode tells which kind of evidence is.
        The kind of evidence affects:
            1. How the evidence is represented.
            2. The prompt to generate the answer.

        By default, the evidence_mode is 0, which means the evidence is plain text with
        no particular semantic representation. The evidence_mode can be:
            1. "table": There will be HTML markup telling that there is a table
                within the evidence.
            2. "chatbot": There will be HTML markup telling that there is a chatbot.
                This chatbot is a scenario, extracted from an Excel file, where each
                row corresponds to an interaction.

        Args:
            question: the original question posed by user
            evidence: the text that contain relevant information to answer the question
                (determined by retrieval pipeline)
            evidence_mode: the mode of evidence, 0 for text, 1 for table, 2 for chatbot
        """
        history = kwargs.get("history", [])
        prompt, evidence, images = self.get_prompt(question, evidence, evidence_mode)

        citation_task = None
        if evidence and self.enable_citation:
            citation_task = asyncio.create_task(
                self.citation_pipeline.ainvoke(context=evidence, question=question)
            )
            print("Citation task created")

        output = ""
        if evidence_mode == EVIDENCE_MODE_FIGURE:
            for text in stream_gpt4v(self.vlm_endpoint, images, prompt, max_tokens=768):
                output += text
                self.report_output(Document(channel="chat", content=text))
                await asyncio.sleep(0)
        else:
            messages = []
            if self.system_prompt:
                messages.append(SystemMessage(content=self.system_prompt))
            for human, ai in history[-self.n_last_interactions :]:
                messages.append(HumanMessage(content=human))
                messages.append(AIMessage(content=ai))
            messages.append(HumanMessage(content=prompt))

            try:
                # try streaming first
                print("Trying LLM streaming")
                for text in self.llm.stream(messages):
                    output += text.text
                    self.report_output(Document(content=text.text, channel="chat"))
                    await asyncio.sleep(0)
            except NotImplementedError:
                print("Streaming is not supported, falling back to normal processing")
                output = self.llm(messages).text
                self.report_output(Document(content=output, channel="chat"))

        # retrieve the citation
        print("Waiting for citation task")
        if citation_task is not None:
            citation = await citation_task
        else:
            citation = None

        answer = Document(text=output, metadata={"citation": citation})

        return answer

    def stream(  # type: ignore
        self, question: str, evidence: str, evidence_mode: int = 0, **kwargs
    ) -> Generator[Document, None, Document]:
        history = kwargs.get("history", [])

        # check if evidence exists, use QA prompt
        if evidence:
            prompt, evidence, images = self.get_prompt(
                question, evidence, evidence_mode
            )
        else:
            prompt = question
            images = []

        output = ""
        logprobs = []
        if evidence_mode == EVIDENCE_MODE_FIGURE:
            for text, _logprobs in stream_gpt4v(
                self.vlm_endpoint, images, prompt, max_tokens=768
            ):
                output += text
                logprobs += _logprobs
                yield Document(channel="chat", content=text)
        else:
            messages = []
            if self.system_prompt:
                messages.append(SystemMessage(content=self.system_prompt))
            for human, ai in history[-self.n_last_interactions :]:
                messages.append(HumanMessage(content=human))
                messages.append(AIMessage(content=ai))
            messages.append(HumanMessage(content=prompt))

            try:
                # try streaming first
                print("Trying LLM streaming")
                for out_msg in self.llm.stream(messages):
                    output += out_msg.text
                    logprobs += out_msg.logprobs
                    yield Document(channel="chat", content=out_msg.text)
            except NotImplementedError:
                print("Streaming is not supported, falling back to normal processing")
                output = self.llm(messages).text
                yield Document(channel="chat", content=output)

        # retrieve the citation
        citation = None
        if evidence and self.enable_citation:
            citation = self.citation_pipeline(context=evidence, question=question)

        if logprobs:
            qa_score = np.exp(np.average(logprobs))
        else:
            qa_score = None

        answer = Document(
            text=output,
            metadata={"citation": citation, "qa_score": qa_score},
        )

        return answer

    def extract_evidence_images(self, evidence: str):
        """Util function to extract and isolate images from context/evidence"""
        image_pattern = r"src='(data:image\/[^;]+;base64[^']+)'"
        matches = re.findall(image_pattern, evidence)
        context = re.sub(image_pattern, "", evidence)
        print(f"Got {len(matches)} images")
        return context, matches


class AddQueryContextPipeline(BaseComponent):

    n_last_interactions: int = 5
    llm: ChatLLM = Node(default_callback=lambda _: llms.get_default())

    def run(self, question: str, history: list) -> Document:
        messages = [
            SystemMessage(
                content="Below is a history of the conversation so far, and a new "
                "question asked by the user that needs to be answered by searching "
                "in a knowledge base.\nYou have access to a Search index "
                "with 100's of documents.\nGenerate a search query based on the "
                "conversation and the new question.\nDo not include cited source "
                "filenames and document names e.g info.txt or doc.pdf in the search "
                "query terms.\nDo not include any text inside [] or <<>> in the "
                "search query terms.\nDo not include any special characters like "
                "'+'.\nIf the question is not in English, rewrite the query in "
                "the language used in the question.\n If the question contains enough "
                "information, return just the number 1\n If it's unnecessary to do "
                "the searching, return just the number 0."
            ),
            HumanMessage(content="How did crypto do last year?"),
            AIMessage(
                content="Summarize Cryptocurrency Market Dynamics from last year"
            ),
            HumanMessage(content="What are my health plans?"),
            AIMessage(content="Show available health plans"),
        ]
        for human, ai in history[-self.n_last_interactions :]:
            messages.append(HumanMessage(content=human))
            messages.append(AIMessage(content=ai))

        messages.append(HumanMessage(content=f"Generate search query for: {question}"))

        resp = self.llm(messages).text
        if resp == "0":
            return Document(content="")

        if resp == "1":
            return Document(content=question)

        return Document(content=resp)


class FullQAPipeline(BaseReasoning):
    """Question answering pipeline. Handle from question to answer"""

    class Config:
        allow_extra = True

    retrievers: list[BaseComponent]

    evidence_pipeline: PrepareEvidencePipeline = PrepareEvidencePipeline.withx()
    answering_pipeline: AnswerWithContextPipeline = AnswerWithContextPipeline.withx()
    rewrite_pipeline: RewriteQuestionPipeline | None = None
    add_query_context: AddQueryContextPipeline = AddQueryContextPipeline.withx()
    trigger_context: int = 150
    use_rewrite: bool = False

    def retrieve(
        self, message: str, history: list
    ) -> tuple[list[RetrievedDocument], list[Document]]:
        """Retrieve the documents based on the message"""
        # if len(message) < self.trigger_context:
        #     # prefer adding context for short user questions, avoid adding context for
        #     # long questions, as they are likely to contain enough information
        #     # plus, avoid the situation where the original message is already too long
        #     # for the model to handle
        #     query = self.add_query_context(message, history).content
        # else:
        #     query = message
        # print(f"Rewritten query: {query}")
        query = None
        if not query:
            # TODO: previously return [], [] because we think this message as something
            # like "Hello", "I need help"...
            query = message

        docs, doc_ids = [], []
        for idx, retriever in enumerate(self.retrievers):
            retriever_node = self._prepare_child(retriever, f"retriever_{idx}")
            for doc in retriever_node(text=query):
                if doc.doc_id not in doc_ids:
                    docs.append(doc)
                    doc_ids.append(doc.doc_id)

        info = []
        for doc in docs:
            if doc.metadata.get("type", "") == "image":
                info.append(
                    Document(
                        channel="info",
                        content=Render.collapsible(
                            header=f"<i>{get_header(doc)}</i>",
                            content=Render.image(
                                url=doc.metadata["image_origin"], text=doc.text
                            ),
                            open=True,
                        ),
                    )
                )
            else:
                info.append(
                    Document(
                        channel="info",
                        content=Render.collapsible(
                            header=f"<i>{get_header(doc)}</i>",
                            content=Render.table(doc.text),
                            open=True,
                        ),
                    )
                )

        return docs, info

    def _format_retrieval_score_and_doc(
        self,
        doc: Document,
        rendered_doc_content: str,
        open_collapsible: bool = False,
    ) -> str:
        """Format the retrieval score and the document"""
        # score from doc_store (Elasticsearch)
        if is_close(doc.score, -1.0):
            vectorstore_score = ""
            text_search_str = " (full-text search)<br>"
        else:
            vectorstore_score = round(doc.score, 2)
            text_search_str = "<br>"

        llm_reranking_score = (
            round(doc.metadata["llm_trulens_score"], 2)
            if doc.metadata.get("llm_trulens_score") is not None
            else 0.0
        )
        cohere_reranking_score = (
            round(doc.metadata["cohere_reranking_score"], 2)
            if doc.metadata.get("cohere_reranking_score") is not None
            else 0.0
        )
        item_type_prefix = doc.metadata.get("type", "")
        item_type_prefix = item_type_prefix.capitalize()
        if item_type_prefix:
            item_type_prefix += " from "

        rendered_score = Render.collapsible(
            header=f"<b>&emsp;Relevance score</b>: {llm_reranking_score}",
            content="<b>&emsp;&emsp;Vectorstore score:</b>"
            f" {vectorstore_score}"
            f"{text_search_str}"
            "<b>&emsp;&emsp;LLM reranking score:</b>"
            f" {llm_reranking_score}<br>"
            "<b>&emsp;&emsp;Cohere reranking score:</b>"
            f" {cohere_reranking_score}<br>",
        )

        return Render.collapsible(
            header=(
                f"<i>{item_type_prefix}{get_header(doc)}</i>"
                f" [score: {llm_reranking_score}]"
            ),
            content=rendered_score + rendered_doc_content,
            open=open_collapsible,
        )

    def prepare_citations(self, answer, docs) -> tuple[list[Document], list[Document]]:
        """Prepare the citations to show on the UI"""
        with_citation, without_citation = [], []
        spans = defaultdict(list)

        if answer.metadata["citation"] and answer.metadata["citation"].answer:
            for fact_with_evidence in answer.metadata["citation"].answer:
                for quote in fact_with_evidence.substring_quote:
                    matched_excerpts = []
                    for doc in docs:
                        matches = find_text(quote, doc.text)

                        for start, end in matches:
                            if "|" not in doc.text[start:end]:
                                spans[doc.doc_id].append(
                                    {
                                        "start": start,
                                        "end": end,
                                    }
                                )
                                matched_excerpts.append(doc.text[start:end])

                    print("Matched citation:", quote, matched_excerpts),

        id2docs = {doc.doc_id: doc for doc in docs}
        not_detected = set(id2docs.keys()) - set(spans.keys())

        # render highlight spans
        for _id, ss in spans.items():
            if not ss:
                not_detected.add(_id)
                continue
            cur_doc = id2docs[_id]
            ss = sorted(ss, key=lambda x: x["start"])
            text = cur_doc.text[: ss[0]["start"]]
            for idx, span in enumerate(ss):
                text += Render.highlight(cur_doc.text[span["start"] : span["end"]])
                if idx < len(ss) - 1:
                    text += cur_doc.text[span["end"] : ss[idx + 1]["start"]]
            text += cur_doc.text[ss[-1]["end"] :]
            # add to display list
            if cur_doc.metadata.get("type", "") == "image":
                with_citation.append(
                    Document(
                        channel="info",
                        content=self._format_retrieval_score_and_doc(
                            cur_doc,
                            Render.image(
                                url=cur_doc.metadata["image_origin"], text=text
                            ),
                            open_collapsible=True,
                        ),
                    )
                )
            else:
                with_citation.append(
                    Document(
                        channel="info",
                        content=self._format_retrieval_score_and_doc(
                            cur_doc,
                            Render.table(text),
                            open_collapsible=True,
                        ),
                    )
                )
        print("Got {} cited docs".format(len(with_citation)))

        sorted_not_detected_items_with_scores = [
            (id_, id2docs[id_].metadata.get("llm_trulens_score", 0.0))
            for id_ in not_detected
        ]
        sorted_not_detected_items_with_scores.sort(key=lambda x: x[1], reverse=True)

        for id_, _ in sorted_not_detected_items_with_scores:
            doc = id2docs[id_]
            if doc.metadata.get("type", "") == "image":
                without_citation.append(
                    Document(
                        channel="info",
                        content=self._format_retrieval_score_and_doc(
                            doc,
                            Render.image(
                                url=doc.metadata["image_origin"], text=doc.text
                            ),
                        ),
                    )
                )
            else:
                without_citation.append(
                    Document(
                        channel="info",
                        content=self._format_retrieval_score_and_doc(
                            doc, Render.table(doc.text)
                        ),
                    )
                )
        return with_citation, without_citation

    async def ainvoke(  # type: ignore
        self, message: str, conv_id: str, history: list, **kwargs  # type: ignore
    ) -> Document:  # type: ignore
        if self.use_rewrite and self.rewrite_pipeline:
            print("Chosen rewrite pipeline", self.rewrite_pipeline)
            rewrite = await self.rewrite_pipeline(question=message)
            message = rewrite.text

        docs, infos = self.retrieve(message, history)
        for _ in infos:
            self.report_output(_)
        await asyncio.sleep(0.1)

        evidence_mode, evidence = self.evidence_pipeline(docs).content
        answer = await self.answering_pipeline(
            question=message,
            history=history,
            evidence=evidence,
            evidence_mode=evidence_mode,
            conv_id=conv_id,
            **kwargs,
        )

        # show the evidence
        with_citation, without_citation = self.prepare_citations(answer, docs)
        if not with_citation and not without_citation:
            self.report_output(Document(channel="info", content="No evidence found.\n"))
        else:
            self.report_output(Document(channel="info", content=None))
            for _ in with_citation:
                self.report_output(_)
            if without_citation:
                for _ in without_citation:
                    self.report_output(_)

        self.report_output(None)
        return answer

    def stream(  # type: ignore
        self, message: str, conv_id: str, history: list, **kwargs  # type: ignore
    ) -> Generator[Document, None, Document]:
        if self.use_rewrite and self.rewrite_pipeline:
            print("Chosen rewrite pipeline", self.rewrite_pipeline)
            message = self.rewrite_pipeline(question=message).text
            print("Rewrite result", message)

        print(f"Retrievers {self.retrievers}")
        # should populate the context
        docs, infos = self.retrieve(message, history)
        print(f"Got {len(docs)} retrieved documents")
        yield from infos

        evidence_mode, evidence = self.evidence_pipeline(docs).content
        answer = yield from self.answering_pipeline.stream(
            question=message,
            history=history,
            evidence=evidence,
            evidence_mode=evidence_mode,
            conv_id=conv_id,
            **kwargs,
        )

        # show the evidence
        with_citation, without_citation = self.prepare_citations(answer, docs)
        if not with_citation and not without_citation:
            yield Document(channel="info", content="<h5><b>No evidence found.</b></h5>")
        else:
            # clear the Info panel
            max_llm_rerank_score = max(
                doc.metadata.get("llm_trulens_score", 0.0) for doc in docs
            )
            # clear previous info
            yield Document(channel="info", content=None)

            # yield warning message
            if max_llm_rerank_score < CONTEXT_RELEVANT_WARNING_SCORE:
                yield Document(
                    channel="info",
                    content=(
                        "<h5>WARNING! Context relevance score is low. "
                        "Double check the model answer for correctness.</h5>"
                    ),
                )

            # show QA score
            qa_score = (
                round(answer.metadata["qa_score"], 2)
                if answer.metadata.get("qa_score")
                else None
            )
            yield Document(
                channel="info",
                content=(f"<h5>Answer confidence: {qa_score}</h5>"),
            )

            yield from with_citation
            if without_citation:
                yield from without_citation

        return answer

    @classmethod
    def get_pipeline(cls, settings, states, retrievers):
        """Get the reasoning pipeline

        Args:
            settings: the settings for the pipeline
            retrievers: the retrievers to use
        """
        prefix = f"reasoning.options.{cls.get_info()['id']}"
        pipeline = cls(
            retrievers=retrievers,
            rewrite_pipeline=RewriteQuestionPipeline(),
        )

        llm_name = settings.get(f"{prefix}.llm", None)
        llm = llms.get(llm_name, llms.get_default())

        # answering pipeline configuration
        answer_pipeline = pipeline.answering_pipeline
        answer_pipeline.llm = llm
        answer_pipeline.citation_pipeline.llm = llm
        answer_pipeline.n_last_interactions = settings[f"{prefix}.n_last_interactions"]
        answer_pipeline.enable_citation = settings[f"{prefix}.highlight_citation"]
        answer_pipeline.system_prompt = settings[f"{prefix}.system_prompt"]
        answer_pipeline.qa_template = settings[f"{prefix}.qa_prompt"]
        answer_pipeline.lang = {"en": "English", "ja": "Japanese"}.get(
            settings["reasoning.lang"], "English"
        )

        pipeline.add_query_context.llm = llm
        pipeline.add_query_context.n_last_interactions = settings[
            f"{prefix}.n_last_interactions"
        ]

        pipeline.trigger_context = settings[f"{prefix}.trigger_context"]
        pipeline.use_rewrite = states.get("app", {}).get("regen", False)
        if pipeline.rewrite_pipeline:
            pipeline.rewrite_pipeline.llm = llm
            pipeline.rewrite_pipeline.lang = {"en": "English", "ja": "Japanese"}.get(
                settings["reasoning.lang"], "English"
            )
        return pipeline

    @classmethod
    def get_user_settings(cls) -> dict:
        from ktem.llms.manager import llms

        llm = ""
        choices = [("(default)", "")]
        try:
            choices += [(_, _) for _ in llms.options().keys()]
        except Exception as e:
            logger.exception(f"Failed to get LLM options: {e}")

        return {
            "llm": {
                "name": "Language model",
                "value": llm,
                "component": "dropdown",
                "choices": choices,
                "special_type": "llm",
                "info": (
                    "The language model to use for generating the answer. If None, "
                    "the application default language model will be used."
                ),
            },
            "highlight_citation": {
                "name": "Highlight Citation",
                "value": True,
                "component": "checkbox",
            },
            "system_prompt": {
                "name": "System Prompt",
                "value": "This is a question answering system",
            },
            "qa_prompt": {
                "name": "QA Prompt (contains {context}, {question}, {lang})",
                "value": DEFAULT_QA_TEXT_PROMPT,
            },
            "n_last_interactions": {
                "name": "Number of interactions to include",
                "value": 5,
                "component": "number",
                "info": "The maximum number of chat interactions to include in the LLM",
            },
            "trigger_context": {
                "name": "Maximum message length for context rewriting",
                "value": 150,
                "component": "number",
                "info": (
                    "The maximum length of the message to trigger context addition. "
                    "Exceeding this length, the message will be used as is."
                ),
            },
        }

    @classmethod
    def get_info(cls) -> dict:
        return {
            "id": "simple",
            "name": "Simple QA",
            "description": (
                "Simple RAG-based question answering pipeline. This pipeline can "
                "perform both keyword search and similarity search to retrieve the "
                "context. After that it includes that context to generate the answer."
            ),
        }


class FullDecomposeQAPipeline(FullQAPipeline):
    def answer_sub_questions(
        self, messages: list, conv_id: str, history: list, **kwargs
    ):
        output_str = ""
        for idx, message in enumerate(messages):
            yield Document(
                channel="chat",
                content=f"<br><b>Sub-question {idx + 1}</b>"
                f"<br>{message}<br><b>Answer</b><br>",
            )
            # should populate the context
            docs, infos = self.retrieve(message, history)
            print(f"Got {len(docs)} retrieved documents")

            yield from infos

            evidence_mode, evidence = self.evidence_pipeline(docs).content
            answer = yield from self.answering_pipeline.stream(
                question=message,
                history=history,
                evidence=evidence,
                evidence_mode=evidence_mode,
                conv_id=conv_id,
                **kwargs,
            )

            output_str += (
                f"Sub-question {idx + 1}-th: '{message}'\nAnswer: '{answer.text}'\n\n"
            )

        return output_str

    def stream(  # type: ignore
        self, message: str, conv_id: str, history: list, **kwargs  # type: ignore
    ) -> Generator[Document, None, Document]:
        sub_question_answer_output = ""
        if self.rewrite_pipeline:
            print("Chosen rewrite pipeline", self.rewrite_pipeline)
            result = self.rewrite_pipeline(question=message)
            print("Rewrite result", result)
            if isinstance(result, Document):
                message = result.text
            elif (
                isinstance(result, list)
                and len(result) > 0
                and isinstance(result[0], Document)
            ):
                yield Document(
                    channel="chat",
                    content="<h4>Sub questions and their answers</h4>",
                )
                sub_question_answer_output = yield from self.answer_sub_questions(
                    [r.text for r in result], conv_id, history, **kwargs
                )

        yield Document(
            channel="chat",
            content=f"<h4>Main question</h4>{message}<br><b>Answer</b><br>",
        )

        # should populate the context
        docs, infos = self.retrieve(message, history)
        print(f"Got {len(docs)} retrieved documents")
        yield from infos

        evidence_mode, evidence = self.evidence_pipeline(docs).content
        answer = yield from self.answering_pipeline.stream(
            question=message,
            history=history,
            evidence=evidence + "\n" + sub_question_answer_output,
            evidence_mode=evidence_mode,
            conv_id=conv_id,
            **kwargs,
        )

        # show the evidence
        with_citation, without_citation = self.prepare_citations(answer, docs)
        if not with_citation and not without_citation:
            yield Document(channel="info", content="<h5><b>No evidence found.</b></h5>")
        else:
            yield Document(channel="info", content=None)
            for _ in with_citation:
                yield _
            if without_citation:
                for _ in without_citation:
                    yield _

        return answer

    @classmethod
    def get_user_settings(cls) -> dict:
        user_settings = super().get_user_settings()
        user_settings["decompose_prompt"] = {
            "name": "Decompose Prompt",
            "value": DecomposeQuestionPipeline.DECOMPOSE_SYSTEM_PROMPT_TEMPLATE,
        }
        return user_settings

    @classmethod
    def get_pipeline(cls, settings, states, retrievers):
        """Get the reasoning pipeline

        Args:
            settings: the settings for the pipeline
            retrievers: the retrievers to use
        """
        prefix = f"reasoning.options.{cls.get_info()['id']}"
        pipeline = cls(
            retrievers=retrievers,
            rewrite_pipeline=DecomposeQuestionPipeline(
                prompt_template=settings.get(f"{prefix}.decompose_prompt")
            ),
        )

        llm_name = settings.get(f"{prefix}.llm", None)
        llm = llms.get(llm_name, llms.get_default())

        # answering pipeline configuration
        answer_pipeline = pipeline.answering_pipeline
        answer_pipeline.llm = llm
        answer_pipeline.citation_pipeline.llm = llm
        answer_pipeline.n_last_interactions = settings[f"{prefix}.n_last_interactions"]
        answer_pipeline.enable_citation = settings[f"{prefix}.highlight_citation"]
        answer_pipeline.system_prompt = settings[f"{prefix}.system_prompt"]
        answer_pipeline.qa_template = settings[f"{prefix}.qa_prompt"]
        answer_pipeline.lang = {"en": "English", "ja": "Japanese"}.get(
            settings["reasoning.lang"], "English"
        )

        pipeline.add_query_context.llm = llm
        pipeline.add_query_context.n_last_interactions = settings[
            f"{prefix}.n_last_interactions"
        ]

        pipeline.trigger_context = settings[f"{prefix}.trigger_context"]
        pipeline.use_rewrite = states.get("app", {}).get("regen", False)
        if pipeline.rewrite_pipeline:
            pipeline.rewrite_pipeline.llm = llm
        return pipeline

    @classmethod
    def get_info(cls) -> dict:
        return {
            "id": "complex",
            "name": "Complex QA",
            "description": (
                "Use multi-step reasoning to decompose a complex question into "
                "multiple sub-questions. This pipeline can "
                "perform both keyword search and similarity search to retrieve the "
                "context. After that it includes that context to generate the answer."
            ),
        }
