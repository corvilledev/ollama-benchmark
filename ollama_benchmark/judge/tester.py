import json
import time
from ollama_benchmark import utils
from ollama_benchmark import errors
from ollama_benchmark.tester import BaseTester

JUDGE_SYSTEM_PROMPT = """
You will be given a user_question and system_answer couple.
Your task is to provide a 'total rating' scoring how well the system_answer answers the user concerns expressed in the user_question.
Give your answer on a scale of 1 to 100, where 1 means that the system_answer is not helpful at all, and 100 means that the system_answer completely and helpfully addresses the user_question.

Here is the scale you should use to build your answer:
10: The system_answer is terrible: completely irrelevant to the question asked, or very partial
30: The system_answer is mostly not helpful: misses some key aspects of the question
60: The system_answer is mostly helpful: provides support, but still could be improved
90: The system_answer is excellent: relevant, direct, detailed, and addresses all the concerns raised in the question

Provide your feedback as JSON as follows with just the result, no other text:

{
    "evaluation": "(your rationale for the rating, as a text)",
    "total_rating": "(your rating, as a number between 1 and 100)",
    "feedback": "(How you think it could be improved)",
}
"""
JUDGE_PROMPT = """
Now here are the question and answer.

Question: {question}
Answer: {answer}
"""


class Tester(BaseTester):
    def __init__(
        self,
        judge_model,
        system_prompt=None,
        judge_system_prompt=None,
        judge_prompt=None,
        ollama_judge_options=None,
        question=None,
        max_turns=1,
        load_messages=None,
        *args, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.judge_model = judge_model
        self.system_prompt = system_prompt
        self.ollama_judge_options = ollama_judge_options,
        self.judge_system_prompt = judge_system_prompt or JUDGE_SYSTEM_PROMPT
        self.judge_prompt = judge_prompt or JUDGE_PROMPT
        self.question = question
        self.max_turns = max_turns
        self.load_messages_file = load_messages

    @property
    def load_messages(self):
        if not hasattr(self, '_load_messages'):
            try:
                self._load_messages = json.load(self.load_messages_file)
            except json.decoder.JSONDecodeError as err:
                msg = f"Invalid JSON messages file: {err}"
                raise errors.ConfigurationError(msg)
        return self._load_messages

    def check_config(self):
        for text in ('{question}', '{answer}'):
            if text not in self.judge_prompt:
                msg = f"The judge prompt must contains '{text}'"
                raise errors.ConfigurationError(msg)
        if self.load_messages_file and not self.load_messages:
            msg = "No loaded messages"
            raise errors.ConfigurationError(msg)

    def get_tasks(self):
        return [self.question]

    def get_tasks_kwargs(self):
        if self.load_messages:
            return [
                (i, {'question_id': 0})
                for i, msg in enumerate(self.load_messages)
            ]
        return [
            (
                task,
                {'question_id': task},
            )
            for task in self.get_tasks()
        ]

    def run_turns(self, question_id):
        data_manager = utils.DataManager()
        question = data_manager.get_question(question_id)
        responses = []
        messages = []
        if self.system_prompt is not None:
            messages.append({
                "role": "system",
                "content": self.system_prompt.read(),
            })

        for turn_id, prompt in enumerate(question['turns'][:self.max_turns]):
            self.logger.debug('turn #%s', turn_id)
            self.logger.info('> %s', prompt)
            message = {
                "role": "user",
                "content": prompt,
            }
            if 'image_urls' in question:
                message['images'] = data_manager.get_question_b64_images(question_id)
            messages.append(message)
            response = self.client.chat(
                model=self.model,
                messages=messages,
                options=self.ollama_options,
            )
            self.logger.info('< %s', response['message']['content'])
            messages.append({
                "role": "assistant",
                "content": response['message']['content'],
            })
            responses.append(response)
        return messages

    def evaluate_turns(self, messages):
        judgements = []
        messages = messages[::]
        if messages and messages[0]['role'] == "system":
            system = messages.pop(0)

        while messages:
            prompt = messages.pop(0)
            answer = messages.pop(0)
            judge_prompt = self.judge_prompt.format(
                question=prompt['content'],
                answer=answer['content'],
                options=self.ollama_judge_options,
            )
            judge_messages = [{
                "role": "system",
                "content": self.judge_system_prompt,
            }, {
                "role": "user",
                "content": judge_prompt,
            }]
            self.logger.debug('system > %s', self.judge_system_prompt)
            self.logger.info('> %s', judge_prompt)
            response = self.client.chat(
                model=self.judge_model,
                messages=judge_messages,
                options=self.ollama_options,
            )
            self.logger.info('< %s', response['message']['content'])
            judgements.append(response)
        return judgements

    def _parse_judgement(self, judgement):
        return json.loads(judgement)

    def run(self, question_id):
        t0 = time.time()
        if self.load_messages:
            messages = self.load_messages[question_id]
        else:
            messages = self.run_turns(question_id)
        message_duration = time.time() - t0

        if (not self.load_messages) and self.model == self.judge_model:
            self.client.unload(self.model)

        t0 = time.time()
        judgements = self.evaluate_turns(messages)
        judge_duration = time.time() - t0

        judgements_content = [
            self._parse_judgement(j['message']['content'])
            for j in judgements
        ]
        result = {
            'messages': messages,
            'judgements': judgements_content,
            'message_duration': message_duration,
            'judge_duration': judge_duration,
            'work_duration': message_duration + judge_duration,
        }
        return result
