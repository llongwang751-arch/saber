from internal.memory.slot_extractor import SlotExtractor
from internal.memory.fast_vector_index import FastVectorIndex


def test_slot_extractor_negative_dislike():
    extractor = SlotExtractor()
    text = "我不喜欢吃香菜，还有我讨厌加班"
    slots = extractor.extract(text)
    
    # 应该提取到 dislike，而不是 like
    dislikes = [s.value for s in slots if s.category == "dislike"]
    likes = [s.value for s in slots if s.category == "like"]
    
    assert any("香菜" in v for v in dislikes)
    assert any("加班" in v for v in dislikes)
    assert not any("香菜" in v for v in likes)


def test_slot_extractor_profession_and_style():
    extractor = SlotExtractor()
    text = "我是算法工程师，请用严谨专业的风格回答"
    slots = extractor.extract(text)
    
    profiles = [s.value for s in slots if s.category == "profile"]
    styles = [s.value for s in slots if s.category == "style"]
    
    assert any("算法工程师" in v for v in profiles)
    assert any("严谨专业" in v for v in styles)


def test_fast_vector_index_search():
    index = FastVectorIndex(dim=4, metric="cosine")
    
    # 添加 3 个向量
    index.add("doc1", [1.0, 0.0, 0.0, 0.0], metadata={"name": "A"})
    index.add("doc2", [0.0, 1.0, 0.0, 0.0], metadata={"name": "B"})
    index.add("doc3", [0.707, 0.707, 0.0, 0.0], metadata={"name": "C"})
    
    assert len(index) == 3
    
    # 检索接近 doc1 的
    results = index.search([1.0, 0.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0][0] == "doc1"
    assert results[0][1] > 0.99
    assert results[1][0] == "doc3"
