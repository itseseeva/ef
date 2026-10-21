with open('src/core/chat/ocr_engine.py', 'r', encoding='utf-8') as f:
    c = f.read()

import re
old_init = r'    def __init__\(self, langs: "tuple\[str, \.\.\.\]" = \("ru", "en"\)\) -> None:\n        import easyocr.*?    def __call__\(self, img: np\.ndarray\) -> str:\n        # detail=0.*?\.strip\(\)'

new_init = '''    def __init__(self, langs: "tuple[str, ...]" = ("ru", "en")) -> None:
        import easyocr
        import collections
        self._r = easyocr.Reader(list(langs), gpu=False, verbose=False)
        self.name = "EasyOCR (%s, CPU)" % "+".join(langs)
        self._cache = collections.OrderedDict()

    def __call__(self, img: np.ndarray) -> str:
        h = hash(img.tobytes())
        if h in self._cache:
            self._cache.move_to_end(h)
            return self._cache[h]
            
        res = " ".join(self._r.readtext(img, detail=0, paragraph=True)).strip()
        
        self._cache[h] = res
        if len(self._cache) > 200:
            self._cache.popitem(last=False)
        return res'''

c = re.sub(old_init, new_init, c, flags=re.DOTALL)

with open('src/core/chat/ocr_engine.py', 'w', encoding='utf-8') as f:
    f.write(c)
