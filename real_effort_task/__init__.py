
from otree.api import *
import random
import time
from .image_utils import encode_image

doc = """
Your app description
"""


def get_task_module(player):
    from . import task_matrix
    return task_matrix

class Constants(BaseConstants):
    name_in_url = 'counting_zeros'
    players_per_group = None
    num_rounds = 1
    # instruction_wage_rate_assignment = '_templates/global/Instruction_wage_rate_assignment.html'
    # instruction_counting_zeros = '_templates/global/Instruction_counting_zeros.html'
    task_params = dict(
        retry_delay=1.0, puzzle_delay=2.0, attempts_per_puzzle=1, max_iterations=10
    )

class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    more_high_wage = models.BooleanField(initial=True)
    wage = models.FloatField() # A player's wage rate
    earning = models.CurrencyField(initial=0) # how much a player earns from the counting task
    iteration = models.IntegerField(initial=0)
    num_trials = models.IntegerField(initial=0) # how many tables a player has tried
    num_correct = models.IntegerField(initial=0) # how many tables a player answers correctly
    num_failed = models.IntegerField(initial=0) # how many tables a player fails


def creating_session(subsession):
    # Draw a wage rate
    for player in subsession.get_players():
        participant = player.participant
        if participant.more_high_wage == True:
            wage_distribution = [75,25]
        else:
            wage_distribution = [25,75]
        player.wage = random.choices([subsession.session.high_wage_rate,subsession.session.low_wage_rate],weights=wage_distribution, k=1)[0]



# puzzle-specific stuff
class Puzzle(ExtraModel):
    """A model to keep record of all generated puzzles"""

    player = models.Link(Player)
    iteration = models.IntegerField(initial=0)
    num_correct = models.IntegerField(initial=0)
    earning = models.CurrencyField(initial=0)
    attempts = models.IntegerField(initial=0)
    timestamp = models.FloatField(initial=0)
    # can be either simple text, or a json-encoded definition of the puzzle, etc.
    text = models.LongStringField()
    # solution may be the same as text, if it's simply a transcription task
    solution = models.LongStringField()
    response = models.LongStringField()
    response_timestamp = models.FloatField()
    is_correct = models.BooleanField()


def generate_puzzle(player: Player) -> Puzzle:
    """Create new puzzle for a player"""
    task_module = get_task_module(player)
    fields = task_module.generate_puzzle_fields()
    player.iteration += 1
    # print(f'puzzle:{fields}')
    return Puzzle.create(
        player=player, iteration=player.iteration, timestamp=time.time(), **fields
    )


def get_current_puzzle(player):
    puzzles = Puzzle.filter(player=player, iteration=player.iteration)
    if puzzles:
        [puzzle] = puzzles
        return puzzle


def encode_puzzle(puzzle: Puzzle):
    """Create data describing puzzle to send to client"""
    task_module = get_task_module(puzzle.player)  # noqa
    # generate image for the puzzle
    image = task_module.render_image(puzzle)
    data = encode_image(image)
    return dict(image=data)


def get_progress(player: Player):
    """Return current player progress"""
    return dict(
        num_trials=player.num_trials,
        num_correct=player.num_correct,
        num_incorrect=player.num_failed,
        earning = player.earning,
        iteration = player.iteration,
    )


def play_game(player: Player, message: dict):
    """Main game workflow
    Implemented as reactive scheme: receive message from vrowser, react, respond.

    Generic game workflow, from server point of view:
    - receive: {'type': 'load'} -- empty message means page loaded
    - check if it's game start or page refresh midgame
    - respond: {'type': 'status', 'progress': ...}
    - respond: {'type': 'status', 'progress': ..., 'puzzle': data} -- in case of midgame page reload

    - receive: {'type': 'next'} -- request for a next/first puzzle
    - generate new puzzle
    - respond: {'type': 'puzzle', 'puzzle': data}

    - receive: {'type': 'answer', 'answer': ...} -- user answered the puzzle
    - check if the answer is correct
    - respond: {'type': 'feedback', 'is_correct': true|false, 'retries_left': ...} -- feedback to the answer

    If allowed by config `attempts_pre_puzzle`, client can send more 'answer' messages
    When done solving, client should explicitely request next puzzle by sending 'next' message

    Field 'progress' is added to all server responses to indicate it on page.

    To indicate max_iteration exhausted in response to 'next' server returns 'status' message with iterations_left=0
    """
    session = player.session
    my_id = player.id_in_group
    task_params = Constants.task_params
    task_module = get_task_module(player)

    now = time.time()
    # the current puzzle or none
    current = get_current_puzzle(player)
    message_type = message['type']
    # page loaded
    if message_type == 'load':
        p = get_progress(player)
        if current:
            return {
                my_id: dict(type='status', progress=p, puzzle=encode_puzzle(current))
            }
        else:
            return {my_id: dict(type='status', progress=p)}

    if message_type == "cheat" and settings.DEBUG:
        return {my_id: dict(type='solution', solution=current.solution)}

    # client requested new puzzle
    if message_type == "next":
        if current is not None:
            if current.response is None:
                raise RuntimeError("trying to skip over unsolved puzzle")
            if now < current.timestamp + task_params["puzzle_delay"]:
                raise RuntimeError("retrying too fast")
            if player.num_correct == task_params['max_iterations']:
                return {
                    my_id: dict(
                        type='status', progress=get_progress(player), iterations_left=0
                    )
                }
        # generate new puzzle
        z = generate_puzzle(player)
        p = get_progress(player)
        return {my_id: dict(type='puzzle', puzzle=encode_puzzle(z), progress=p)}

    # client gives an answer to current puzzle
    if message_type == "answer":
        if current is None:
            raise RuntimeError("trying to answer no puzzle")

        if current.response is not None:  # it's a retry
            if current.attempts >= task_params["attempts_per_puzzle"]:
                raise RuntimeError("no more attempts allowed")
            if now < current.response_timestamp + task_params["retry_delay"]:
                raise RuntimeError("retrying too fast")

            # undo last updation of player progress
            player.num_trials -= 1
            if current.is_correct:
                player.num_correct -= 1
            else:
                player.num_failed -= 1

        # check answer
        answer = message["answer"]

        if answer == "" or answer is None:
            raise ValueError("bogus answer")

        current.response = answer
        current.is_correct = task_module.is_correct(answer, current)
        current.response_timestamp = now
        current.attempts += 1

        # update player progress
        if current.is_correct:
            player.num_correct += 1
            player.earning = cu(player.num_correct*player.wage)
        else:
            player.num_failed += 1
        player.num_trials += 1

        retries_left = task_params["attempts_per_puzzle"] - current.attempts
        p = get_progress(player)
        return {
            my_id: dict(
                type='feedback',
                is_correct=current.is_correct,
                retries_left=retries_left,
                progress=p,
            )
        }

    raise RuntimeError("unrecognized message from client")


# PAGES
class Drawing_wage_rate(Page):
    @staticmethod
    def vars_for_template(player):
        if player.more_high_wage == True:
            p_lowwage = '25%'
            p_highwage = '75%'
        elif player.more_high_wage == False:
            p_lowwage = '75%'
            p_highwage = '25%'
        return dict(
            p_lowwage=p_lowwage, p_highwage=p_highwage
        )    

class Counting_zeros_instruction(Page):
    pass


class Counting_zeros_task(Page):
    live_method = play_game
    @staticmethod
    def js_vars(player: Player):
        return dict(params=Constants.task_params)

    @staticmethod
    def vars_for_template(player: Player):
        task_module = get_task_module(player)
        return dict(input_type=task_module.INPUT_TYPE,
                    placeholder=task_module.INPUT_HINT)


class Counting_zeros_result(Page):
    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        player.participant.wage = player.wage
        player.participant.earning = player.earning
        player.participant.wait_page_arrival = time.time()
              


page_sequence = [Drawing_wage_rate,Counting_zeros_instruction,Counting_zeros_task,Counting_zeros_result]
