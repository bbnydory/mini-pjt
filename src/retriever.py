"""retriever.py - 훈련 이론 문서 RAG 파이프라인.

색인:  python -m src.retriever --build
시험:  python -m src.retriever "테이퍼링은 언제 시작해?"

청킹은 문서의 소제목(##) 단위로 한다. 한 소제목이 하나의 주제를 다루도록 문서를 썼기 때문에
글자 수로 자르는 것보다 검색 단위가 의미와 맞는다. 각 청크 앞에 "문서 제목 > 소제목"을 붙여
청크만 떼어 읽어도 무엇에 대한 내용인지 알 수 있게 했다.

검색 방식 (config.RETRIEVER_MODE):
  vector  벡터 검색만 (1차 평가 기준선)
  hybrid  BM25(한국어 형태소) + 벡터 앙상블
"""
import shutil
import sys
import threading

from langchain_core.documents import Document

from src.config import (
    CHROMA_DIR,
    COLLECTION_NAME,
    DOCS_DIR,
    EMBED_MODEL_ID,
    REGION,
    RETRIEVER_EXPAND_MIN_HITS,
    RETRIEVER_K,
    RETRIEVER_MAX_CHUNKS,
    RETRIEVER_MODE,
)


def load_chunks() -> list[Document]:
    """data/docs 의 마크다운을 소제목 단위 청크로 나눈다."""
    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), path.stem)
        section, body, index = None, [], 0

        def flush():
            nonlocal index
            text = "\n".join(body).strip()
            if section and text:
                index += 1
                chunks.append(Document(
                    page_content=f"{title} > {section}\n{text}",
                    metadata={
                        "doc_id": f"{path.stem}#{index}",
                        "source": path.name,
                        "title": title,
                        "section": section,
                    },
                ))

        for ln in lines:
            if ln.startswith("## "):
                flush()
                section, body = ln[3:].strip(), []
            elif section is not None:
                body.append(ln)
        flush()
    return chunks


def _embeddings():
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(model_id=EMBED_MODEL_ID, region_name=REGION)


def _build_index_unlocked() -> int:
    from langchain_chroma import Chroma

    chunks = load_chunks()
    if CHROMA_DIR.exists():
        shutil.rmtree(CHROMA_DIR)
    Chroma.from_documents(
        documents=chunks,
        embedding=_embeddings(),
        ids=[c.metadata["doc_id"] for c in chunks],
        collection_name=COLLECTION_NAME,
        persist_directory=str(CHROMA_DIR),
    )
    return len(chunks)


def build_index() -> int:
    """문서를 임베딩해 벡터 DB를 새로 만든다. 만든 청크 수를 돌려준다."""
    global _db, _hybrid
    with _db_lock:
        count = _build_index_unlocked()
        _db, _hybrid = None, None
    return count


_db = None
_db_lock = threading.Lock()


def get_db():
    """벡터 DB를 연다. 아직 없으면 한 번 만든다.

    검색 도구는 여러 스레드에서 동시에 불릴 수 있다. 잠금 없이 두 스레드가 같은 폴더의 DB를
    동시에 처음 열면 Chroma가 KeyError 등으로 실패했으므로, 처음 여는 구간을 잠금으로 감쌌다.
    """
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                from langchain_chroma import Chroma

                if not CHROMA_DIR.exists():
                    _build_index_unlocked()
                _db = Chroma(
                    persist_directory=str(CHROMA_DIR),
                    embedding_function=_embeddings(),
                    collection_name=COLLECTION_NAME,
                )
    return _db


_hybrid = None


def _hybrid_retriever():
    """BM25 + 벡터 앙상블 검색기 (2일차 hybrid.py 구성). 처음 한 번만 만든다."""
    global _hybrid
    if _hybrid is None:
        vector_db = get_db()
        with _db_lock:
            if _hybrid is None:
                _hybrid = _make_hybrid(vector_db)
    return _hybrid


def _make_hybrid(vector_db):
    from kiwipiepy import Kiwi
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_community.retrievers import BM25Retriever

    kiwi = Kiwi()
    keep = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}

    def korean_tokenizer(text: str) -> list[str]:
        return [t.form for t in kiwi.tokenize(text) if t.tag in keep]

    bm25 = BM25Retriever.from_documents(load_chunks(), preprocess_func=korean_tokenizer)
    bm25.k = RETRIEVER_K
    vector = vector_db.as_retriever(search_kwargs={"k": RETRIEVER_K})
    return EnsembleRetriever(retrievers=[bm25, vector], weights=[0.4, 0.6])


def _expand_by_document(docs: list[Document]) -> list[Document]:
    """한 문서에서 조각이 여러 개 잡히면 그 문서의 나머지 소제목을 뒤에 덧붙인다.

    질문이 문서 하나의 주제 전체를 묻는 경우, 질문 문장과 표현이 다른 소제목이 빠지는 것을 막는다.
    원래 검색 순서는 유지하고, 덧붙인 조각은 문서 안의 순서대로 둔다.
    """
    if RETRIEVER_EXPAND_MIN_HITS <= 0:
        return docs
    hits: dict[str, int] = {}
    for d in docs:
        hits[d.metadata["source"]] = hits.get(d.metadata["source"], 0) + 1
    wanted = [src for src, n in hits.items() if n >= RETRIEVER_EXPAND_MIN_HITS]
    if not wanted:
        return docs
    seen = {d.metadata["doc_id"] for d in docs}
    out = list(docs)
    for chunk in _all_chunks():
        if len(out) >= RETRIEVER_MAX_CHUNKS:
            break
        if chunk.metadata["source"] in wanted and chunk.metadata["doc_id"] not in seen:
            seen.add(chunk.metadata["doc_id"])
            out.append(chunk)
    return out


_chunks_cache: list[Document] | None = None


def _all_chunks() -> list[Document]:
    """전체 청크 목록. 파일을 매번 다시 읽지 않도록 한 번만 만든다."""
    global _chunks_cache
    if _chunks_cache is None:
        _chunks_cache = load_chunks()
    return _chunks_cache


def search(query: str, k: int = RETRIEVER_K, mode: str | None = None) -> list[dict]:
    """질문과 관련된 문서 청크를 찾아 dict 목록으로 돌려준다."""
    mode = mode or RETRIEVER_MODE
    if mode == "hybrid":
        docs = _hybrid_retriever().invoke(query)[:k]
    else:
        docs = get_db().similarity_search(query, k=k)
    docs = _expand_by_document(docs)
    return [
        {
            "doc_id": d.metadata["doc_id"],
            "source": d.metadata["source"],
            "title": d.metadata["title"],
            "section": d.metadata["section"],
            "text": d.page_content,
        }
        for d in docs
    ]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--build":
        print(f"색인 완료: {build_index()}개 청크 -> {CHROMA_DIR}")
    else:
        question = " ".join(sys.argv[1:]) or "장거리주는 왜 필요해?"
        print(f"질문: {question}  (방식: {RETRIEVER_MODE})")
        for hit in search(question):
            print(f"- [{hit['doc_id']}] {hit['title']} > {hit['section']}")
