import unittest
from types import SimpleNamespace
from app.vimax_engine import screenwriter_class

class CapturingChat:
    def __init__(self, content): self.content=content;self.messages=[]
    async def ainvoke(self, messages): self.messages=messages;return SimpleNamespace(content=self.content)

class ScreenwriterPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_movie_prompt_is_preserved(self):
        chat=CapturingChat('A story')
        await screenwriter_class()(chat).develop_story('A concept')
        self.assertIn('creative story generation',chat.messages[0][1])

    async def test_dialogue_prompt_overrides_movie_instructions(self):
        chat=CapturingChat('{"script":["001|坐席|0|你好"]}')
        result=await screenwriter_class()(chat).write_script_based_on_story('plan',system_prompt='Dialogue only. {format_instructions}')
        self.assertEqual(result,['001|坐席|0|你好'])
        self.assertTrue(chat.messages[0][1].startswith('Dialogue only.'))
        self.assertNotIn('filmable',chat.messages[0][1])

if __name__=='__main__':unittest.main()
