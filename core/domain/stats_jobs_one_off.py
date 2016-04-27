# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Jobs for statistics views."""

import ast
import collections
import itertools
import re

from core import jobs
from core.domain import exp_domain
from core.domain import exp_services
from core.domain import rule_domain
from core.domain import stats_jobs_continuous
from core.domain import stats_domain
from core.domain import stats_services
from core.platform import models
from extensions.objects.models import objects

import utils

(base_models, stats_models, exp_models,) = models.Registry.import_models([
    models.NAMES.base_model, models.NAMES.statistics, models.NAMES.exploration
])
transaction_services = models.Registry.import_transaction_services()


# pylint: disable=W0123
class StatisticsAudit(jobs.BaseMapReduceJobManager):

    _STATE_COUNTER_ERROR_KEY = 'State Counter ERROR'

    @classmethod
    def entity_classes_to_map_over(cls):
        return [
            stats_models.ExplorationAnnotationsModel,
            stats_models.StateCounterModel]

    @staticmethod
    def map(item):
        if isinstance(item, stats_models.StateCounterModel):
            if item.first_entry_count < 0:
                yield (
                    StatisticsAudit._STATE_COUNTER_ERROR_KEY,
                    'Less than 0: %s %d' % (item.key, item.first_entry_count))
            return
        # Older versions of ExplorationAnnotations didn't store exp_id
        # This is short hand for making sure we get ones updated most recently
        else:
            if item.exploration_id is not None:
                yield (item.exploration_id, {
                    'version': item.version,
                    'starts': item.num_starts,
                    'completions': item.num_completions,
                    'state_hit': item.state_hit_counts
                })

    @staticmethod
    def reduce(key, stringified_values):
        if key == StatisticsAudit._STATE_COUNTER_ERROR_KEY:
            for value_str in stringified_values:
                yield (value_str,)
            return

        # If the code reaches this point, we are looking at values that
        # correspond to each version of a particular exploration.

        # These variables correspond to the VERSION_ALL version.
        all_starts = 0
        all_completions = 0
        all_state_hit = collections.defaultdict(int)

        # These variables correspond to the sum of counts for all other
        # versions besides VERSION_ALL.
        sum_starts = 0
        sum_completions = 0
        sum_state_hit = collections.defaultdict(int)

        for value_str in stringified_values:
            value = ast.literal_eval(value_str)
            if value['starts'] < 0:
                yield (
                    'Negative start count: exp_id:%s version:%s starts:%s' %
                    (key, value['version'], value['starts']),)

            if value['completions'] < 0:
                yield (
                    'Negative completion count: exp_id:%s version:%s '
                    'completions:%s' %
                    (key, value['version'], value['completions']),)

            if value['completions'] > value['starts']:
                yield ('Completions > starts: exp_id:%s version:%s %s>%s' % (
                    key, value['version'], value['completions'],
                    value['starts']),)

            if value['version'] == stats_jobs_continuous.VERSION_ALL:
                all_starts = value['starts']
                all_completions = value['completions']
                for (state_name, counts) in value['state_hit'].iteritems():
                    all_state_hit[state_name] = counts['first_entry_count']
            else:
                sum_starts += value['starts']
                sum_completions += value['completions']
                for (state_name, counts) in value['state_hit'].iteritems():
                    sum_state_hit[state_name] += counts['first_entry_count']

        if sum_starts != all_starts:
            yield (
                'Non-all != all for starts: exp_id:%s sum: %s all: %s'
                % (key, sum_starts, all_starts),)
        if sum_completions != all_completions:
            yield (
                'Non-all != all for completions: exp_id:%s sum: %s all: %s'
                % (key, sum_completions, all_completions),)

        for state_name in all_state_hit:
            if (state_name not in sum_state_hit and
                    all_state_hit[state_name] != 0):
                yield (
                    'state hit count not same exp_id:%s state:%s, '
                    'all:%s sum: null' % (
                        key, state_name, all_state_hit[state_name]),)
            elif all_state_hit[state_name] != sum_state_hit[state_name]:
                yield (
                    'state hit count not same exp_id: %s state: %s '
                    'all: %s sum:%s' % (
                        key, state_name, all_state_hit[state_name],
                        sum_state_hit[state_name]),)


class AnswersAudit(jobs.BaseMapReduceJobManager):

    # pylint: disable=invalid-name
    _STATE_COUNTER_ERROR_KEY = 'State Counter ERROR'
    _UNKNOWN_HANDLER_NAME_COUNTER_KEY = 'UnknownHandlerCounter'
    _SUBMIT_HANDLER_NAME_COUNTER_KEY = 'SubmitHandlerCounter'
    _HANDLER_FUZZY_RULE_COUNTER_KEY = 'FuzzyRuleCounter'
    _HANDLER_DEFAULT_RULE_COUNTER_KEY = 'DefaultRuleCounter'
    _HANDLER_STANDARD_RULE_COUNTER_KEY = 'StandardRuleCounter'
    _STANDARD_RULE_SUBMISSION_COUNTER_KEY = 'StandardRuleSubmitCounter'
    _HANDLER_ERROR_RULE_COUNTER_KEY = 'ErrorRuleCounter'
    _UNIQUE_ANSWER_COUNTER_KEY = 'UniqueAnswerCounter'
    _CUMULATIVE_ANSWER_COUNTER_KEY = 'CumulativeAnswerCounter'

    @classmethod
    def _get_consecutive_dot_count(cls, string, idx):
        for i in range(idx, len(string)):
            if string[i] != '.':
                return i - idx
        return 0

    @classmethod
    def entity_classes_to_map_over(cls):
        return [stats_models.StateRuleAnswerLogModel]

    @staticmethod
    def map(item):
        item_id = item.id
        if 'submit' not in item_id:
            yield (AnswersAudit._UNKNOWN_HANDLER_NAME_COUNTER_KEY, {
                'reduce_type': AnswersAudit._UNKNOWN_HANDLER_NAME_COUNTER_KEY,
                'rule_spec_str': item.id
            })
            return

        period_idx = item_id.index('submit')
        item_id = item_id[period_idx:]
        period_idx = item_id.index('.')
        period_idx += (
            AnswersAudit._get_consecutive_dot_count(item_id, period_idx) - 1)
        handler_name = item_id[:period_idx]
        yield (handler_name, {
            'reduce_type': AnswersAudit._SUBMIT_HANDLER_NAME_COUNTER_KEY,
            'rule_spec_str': item.id
        })

        item_id = item_id[period_idx+1:]
        rule_str = item_id

        answers = item.answers
        total_submission_count = 0
        for _, count in answers.iteritems():
            total_submission_count += count
            yield (AnswersAudit._UNIQUE_ANSWER_COUNTER_KEY, {
                'reduce_type': AnswersAudit._UNIQUE_ANSWER_COUNTER_KEY
            })
            for _ in xrange(count):
                yield (AnswersAudit._CUMULATIVE_ANSWER_COUNTER_KEY, {
                    'reduce_type': AnswersAudit._CUMULATIVE_ANSWER_COUNTER_KEY
                })

        if rule_str == 'FuzzyMatches':
            for _ in xrange(total_submission_count):
                yield (rule_str, {
                    'reduce_type': AnswersAudit._HANDLER_FUZZY_RULE_COUNTER_KEY
                })
        elif rule_str == 'Default':
            for _ in xrange(total_submission_count):
                yield (rule_str, {
                    'reduce_type': (
                        AnswersAudit._HANDLER_DEFAULT_RULE_COUNTER_KEY)
                })
        elif '(' in rule_str and rule_str[-1] == ')':
            index = rule_str.index('(')
            rule_type = rule_str[0:index]
            rule_args = rule_str[index+1:-1]
            for _ in xrange(total_submission_count):
                yield (rule_type, {
                    'reduce_type': (
                        AnswersAudit._HANDLER_STANDARD_RULE_COUNTER_KEY),
                    'rule_str': rule_str,
                    'rule_args': rule_args
                })
            for _ in xrange(total_submission_count):
                yield (AnswersAudit._STANDARD_RULE_SUBMISSION_COUNTER_KEY, {
                    'reduce_type': (
                        AnswersAudit._STANDARD_RULE_SUBMISSION_COUNTER_KEY)
                })
        else:
            for _ in xrange(total_submission_count):
                yield (rule_str, {
                    'reduce_type': AnswersAudit._HANDLER_ERROR_RULE_COUNTER_KEY
                })

    @staticmethod
    def reduce(key, stringified_values):
        reduce_type = None
        reduce_count = len(stringified_values)
        for value_str in stringified_values:
            value_dict = ast.literal_eval(value_str)
            if reduce_type and reduce_type != value_dict['reduce_type']:
                yield 'Internal error 1'
            elif not reduce_type:
                reduce_type = value_dict['reduce_type']

        if reduce_type == AnswersAudit._UNKNOWN_HANDLER_NAME_COUNTER_KEY:
            rule_spec_strs = [
                ast.literal_eval(value_str)['rule_spec_str']
                for value_str in stringified_values
            ]
            yield (
                'Encountered unknown handler %d time(s), FOUND RULE SPEC '
                'STRINGS: \n%s' % (reduce_count, rule_spec_strs[:10]))
        elif reduce_type == AnswersAudit._SUBMIT_HANDLER_NAME_COUNTER_KEY:
            yield 'Found handler "%s" %d time(s)' % (key, reduce_count)
        elif reduce_type == AnswersAudit._HANDLER_FUZZY_RULE_COUNTER_KEY:
            yield 'Found fuzzy rules %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit._HANDLER_DEFAULT_RULE_COUNTER_KEY:
            yield 'Found default rules %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit._HANDLER_STANDARD_RULE_COUNTER_KEY:
            yield 'Found rule type "%s" %d time(s)' % (key, reduce_count)
        elif reduce_type == AnswersAudit._STANDARD_RULE_SUBMISSION_COUNTER_KEY:
            yield 'Standard rule submitted %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit._HANDLER_ERROR_RULE_COUNTER_KEY:
            yield (
                'Encountered invalid rule string %d time(s) (is it too long?): '
                '"%s"' % (reduce_count, key))
        elif reduce_type == AnswersAudit._UNIQUE_ANSWER_COUNTER_KEY:
            yield 'Total of %d unique answers' % reduce_count
        elif reduce_type == AnswersAudit._CUMULATIVE_ANSWER_COUNTER_KEY:
            yield 'Total of %d answers have been submitted' % reduce_count
        else:
            yield 'Internal error 2'


class AnswersAudit2(jobs.BaseMapReduceJobManager):

    # pylint: disable=invalid-name
    _HANDLER_FUZZY_RULE_COUNTER_KEY = 'FuzzyRuleCounter'
    _HANDLER_DEFAULT_RULE_COUNTER_KEY = 'DefaultRuleCounter'
    _HANDLER_STANDARD_RULE_COUNTER_KEY = 'StandardRuleCounter'
    _STANDARD_RULE_SUBMISSION_COUNTER_KEY = 'StandardRuleSubmitCounter'
    _HANDLER_ERROR_RULE_COUNTER_KEY = 'ErrorRuleCounter'
    _CUMULATIVE_ANSWER_COUNTER_KEY = 'CumulativeAnswerCounter'

    @classmethod
    def entity_classes_to_map_over(cls):
        return [stats_models.StateAnswersModel]

    @staticmethod
    def map(item):
        for answer in item.answers_list:
            yield (AnswersAudit2._CUMULATIVE_ANSWER_COUNTER_KEY, {
                'reduce_type': AnswersAudit2._CUMULATIVE_ANSWER_COUNTER_KEY
            })
            rule_str = answer['rule_spec_str']
            if rule_str == 'FuzzyMatches':
                yield (rule_str, {
                    'reduce_type': AnswersAudit2._HANDLER_FUZZY_RULE_COUNTER_KEY
                })
            elif rule_str == 'Default':
                yield (rule_str, {
                    'reduce_type': (
                        AnswersAudit2._HANDLER_DEFAULT_RULE_COUNTER_KEY)
                })
            elif '(' in rule_str and rule_str[-1] == ')':
                index = rule_str.index('(')
                rule_type = rule_str[0:index]
                rule_args = rule_str[index+1:-1]
                yield (rule_type, {
                    'reduce_type': (
                        AnswersAudit2._HANDLER_STANDARD_RULE_COUNTER_KEY),
                    'rule_str': rule_str,
                    'rule_args': rule_args
                })
                yield (AnswersAudit2._STANDARD_RULE_SUBMISSION_COUNTER_KEY, {
                    'reduce_type': (
                        AnswersAudit2._STANDARD_RULE_SUBMISSION_COUNTER_KEY)
                })
            else:
                yield (rule_str, {
                    'reduce_type': AnswersAudit2._HANDLER_ERROR_RULE_COUNTER_KEY
                })

    @staticmethod
    def reduce(key, stringified_values):
        reduce_type = None
        reduce_count = len(stringified_values)
        for value_str in stringified_values:
            value_dict = ast.literal_eval(value_str)
            if reduce_type and reduce_type != value_dict['reduce_type']:
                yield 'Internal error 1'
            elif not reduce_type:
                reduce_type = value_dict['reduce_type']

        if reduce_type == AnswersAudit2._HANDLER_FUZZY_RULE_COUNTER_KEY:
            yield 'Found fuzzy rules %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit2._HANDLER_DEFAULT_RULE_COUNTER_KEY:
            yield 'Found default rules %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit2._HANDLER_STANDARD_RULE_COUNTER_KEY:
            yield 'Found rule type "%s" %d time(s)' % (key, reduce_count)
        elif reduce_type == AnswersAudit2._STANDARD_RULE_SUBMISSION_COUNTER_KEY:
            yield 'Standard rule submitted %d time(s)' % reduce_count
        elif reduce_type == AnswersAudit2._HANDLER_ERROR_RULE_COUNTER_KEY:
            yield (
                'Encountered invalid rule string %d time(s) (is it too long?): '
                '"%s"' % (reduce_count, key))
        elif reduce_type == AnswersAudit2._CUMULATIVE_ANSWER_COUNTER_KEY:
            yield 'Total of %d answers have been submitted' % reduce_count
        else:
            yield 'Internal error 2'


class AnswerMigrationJob(jobs.BaseMapReduceJobManager):
    """This job is responsible for migrating all answers stored within
    stats_models.StateRuleAnswerLogModel to stats_models.StateAnswersModel
    """
    _ERROR_KEY = 'Answer Migration ERROR'

    _DEFAULT_RULESPEC_STR = 'Default'

    _RECONSTITUTION_FUNCTION_MAP = {
        'CodeRepl': '_cb_reconstitute_code_evaluation',
        'Continue': '_cb_reconstitute_continue',
        'EndExploration': '_cb_reconstitute_end_exploration',
        'GraphInput': '_cb_reconstitute_graph_input',
        'ImageClickInput': '_cb_reconstitute_image_click_input',
        'InteractiveMap': '_cb_reconstitute_interactive_map',
        'ItemSelectionInput': '_cb_reconstitute_item_selection_input',
        'LogicProof': '_cb_reconstitute_logic_proof',
        'MathExpressionInput': '_cb_reconstitute_math_expression_input',
        'MultipleChoiceInput': '_cb_reconstitute_multiple_choice_input',
        'MusicNotesInput': '_cb_reconstitute_music_notes_input',
        'NumericInput': '_cb_reconstitute_numeric_input',
        'PencilCodeEditor': '_cb_reconstitute_pencil_code_editor',
        'SetInput': '_cb_reconstitute_set_input',
        'TextInput': '_cb_reconstitute_text_input',
    }

    _EXPECTED_NOTE_TYPES = [
        'C4', 'D4', 'E4', 'F4', 'G4', 'A4', 'B4', 'C5', 'D5', 'E5', 'F5', 'G5',
        'A5'
    ]

    # Following are all rules in Oppia during the time of answer migration. (44)
    # Each being migrated by this job is prefixed with a '+' and, conversely, a
    # prefix of '-' indicates it is not being migrated by this job. 6 rules are
    # not being recovered. Also, rules which cannot be 100% recovered are noted.
    # + checked_proof.Correct
    # - checked_proof.NotCorrect
    # - checked_proof.NotCorrectByCategory
    # + click_on_image.IsInRegion
    # - code_evaluation.CodeEquals
    # + code_evaluation.CodeContains (cannot be 100% recovered)
    # + code_evaluation.CodeDoesNotContain (cannot be 100% recovered)
    # + code_evaluation.OutputEquals (cannot be 100% recovered)
    # + code_evaluation.ResultsInError (cannot be 100% recovered)
    # - code_evaluation.ErrorContains
    # + coord_two_dim.Within
    # + coord_two_dim.NotWithin
    # - graph.HasGraphProperty
    # - graph.IsIsomorphicTo
    # + math_expression.IsMathematicallyEquivalentTo
    # + music_phrase.Equals
    # + music_phrase.IsLongerThan
    # + music_phrase.HasLengthInclusivelyBetween
    # + music_phrase.IsEqualToExceptFor
    # + music_phrase.IsTranspositionOf
    # + music_phrase.IsTranspositionOfExceptFor
    # + nonnegative_int.Equals
    # + normalized_string.Equals
    # + normalized_string.CaseSensitiveEquals
    # + normalized_string.StartsWith
    # + normalized_string.Contains
    # + normalized_string.FuzzyEquals
    # + real.Equals
    # + real.IsLessThan
    # + real.IsGreaterThan
    # + real.IsLessThanOrEqualTo
    # + real.IsGreaterThanOrEqualTo
    # + real.IsInclusivelyBetween
    # + real.IsWithinTolerance
    # + set_of_html_string.Equals
    # + set_of_html_string.ContainsAtLeastOneOf
    # + set_of_html_string.DoesNotContainAtLeastOneOf
    # + set_of_unicode_string.Equals
    # + set_of_unicode_string.IsSubsetOf
    # + set_of_unicode_string.IsSupersetOf
    # + set_of_unicode_string.HasElementsIn
    # + set_of_unicode_string.HasElementsNotIn
    # + set_of_unicode_string.OmitsElementsIn
    # + set_of_unicode_string.IsDisjointFrom

    # NOTE TO DEVELOPERS: This was never a modifiable value, so it will always
    # take the minimum value. It wasn't stored in answers, but it does not need
    # to be reconstituted.
    _NOTE_DURATION_FRACTION_PART = 1

    @classmethod
    def _find_exploration_immediately_before_timestamp(cls, exp_id, when):
        # Find the latest exploration version before the given time.

        # NOTE(bhenning): This depends on ExplorationCommitLogEntryModel, which
        # was added in ecbfff0. This means any data added before that time will
        # assume to be matched to the earliest recorded commit.

        # NOTE(bhenning): Also, it's possible some of these answers were
        # submitted during a playthrough where the exploration was changed
        # midway. There's not a lot that can be done about this; hopefully the
        # job can convert the answer correctly or detect if it can't. If this
        # ends up being a major issue, it might be mitigated by scanning stats
        # around the time the answer was submitted, but it's not possible to
        # demultiplex the stream of answers and identify which session they are
        # associated with.

        latest_exp_model = exp_models.ExplorationModel.get(exp_id)
        if (latest_exp_model.version == 1 or
                latest_exp_model.last_updated < when):
            # Short-circuit: the answer was submitted later than the current
            # exp version. Otherwise, this is the only version and something is
            # wrong with the answer. Just deal with it.
            return exp_services.get_exploration_from_model(latest_exp_model)

        # TODO(bhenning): Convert calls to CommitLogEntry to own model.

        # Look backwards in the history of the exploration, starting with the
        # latest version.
        for version in reversed(range(latest_exp_model.version)):
            exp_commit_model = exp_models.ExplorationCommitLogEntryModel.get(
                'exploration-%s-%s' % (exp_id, version))
            if exp_commit_model.created_on < when:
                # Found the closest exploration to the given
                exp_model = exp_models.ExplorationModel.get(
                    exp_id, version=version)
                return exp_services.get_exploration_from_model(exp_model)

        # This indicates a major issue, also. Just return the latest version.
        return exp_services.get_exploration_from_model(latest_exp_model)

    # This function comes from extensions.answer_summarizers.models.
    @classmethod
    def _get_hashable_value(cls, value):
        """This function returns a hashable version of the input value. If the
        value itself is hashable, it simply returns that value. If it's a list,
        it will return a tuple with all of the list's elements converted to
        hashable types. If it's a dictionary, it will first convert it to a list
        of pairs, where the key and value of the pair are converted to hashable
        types, then it will convert this list as any other list would be
        converted.
        """
        if isinstance(value, list):
            # Avoid needlessly wrapping a single value in a tuple.
            if len(value) == 1:
                return cls._get_hashable_value(value[0])
            return tuple([cls._get_hashable_value(elem) for elem in value])
        elif isinstance(value, dict):
            return cls._get_hashable_value(
                [(cls._get_hashable_value(key), cls._get_hashable_value(value))
                 for (key, value) in value.iteritems()])
        else:
            return value

    @classmethod
    def _stringify_classified_rule(cls, rule_spec):
        # This is based on the original
        # exp_domain.RuleSpec.stringify_classified_rule, however it returns a
        # list of possible matches by permuting the rule_spec inputs, since the
        # order of a Python dict is implementation-dependent. Our stringified
        # string may not necessarily match the one stored a long time ago in
        # the data store.
        if rule_spec.rule_type == rule_domain.FUZZY_RULE_TYPE:
            yield rule_spec.rule_type
        else:
            rule_spec_inputs = rule_spec.inputs.values()
            for permuted_input in itertools.permutations(rule_spec_inputs):
                param_list = [utils.to_ascii(val) for val in permuted_input]
                yield '%s(%s)' % (rule_spec.rule_type, ','.join(param_list))

    @classmethod
    def _infer_which_answer_group_and_rule_match_answer(cls, state, rule_str):
        # First, check whether it matches against the default rule, which
        # thereby translates to the default outcome.
        answer_groups = state.interaction.answer_groups
        if rule_str == cls._DEFAULT_RULESPEC_STR:
            return (len(answer_groups), 0)

        # Otherwise, first RuleSpec instance to match is the winner. The first
        # pass is to stringify parameters and doing a string comparison. This is
        # efficient and works for most situations.
        for answer_group_index, answer_group in enumerate(answer_groups):
            rule_specs = answer_group.rule_specs
            for rule_spec_index, rule_spec in enumerate(rule_specs):
                possible_stringified_rules = list(
                    cls._stringify_classified_rule(rule_spec))
                if rule_str in possible_stringified_rules:
                    return (answer_group_index, rule_spec_index)

        # The second attempt involves parsing the rule string and doing an exact
        # match on the rule parameter values. This needs to be done in the event
        # that the Python-turned-ascii parameters have their own elements out of
        # order (such as with a dict parameter).
        if '(' in rule_str and rule_str[-1] == ')':
            # http://stackoverflow.com/questions/9623114
            unordered_lists_equal = lambda x, y: (
                collections.Counter(
                    AnswerMigrationJob._get_hashable_value(x)) ==
                collections.Counter(
                    AnswerMigrationJob._get_hashable_value(y)))

            paren_index = rule_str.index('(')
            rule_type = rule_str[:paren_index]
            param_str_list_str = rule_str[paren_index+1:-1]
            partial_param_str_list = param_str_list_str.split(',')
            param_str_list = []

            # Correctly split the parameter list by correcting the results from
            # naively splitting it by merging subsequent elements in the list if
            # the comma fell within brackets or parentheses.
            concat_with_previous = False
            open_group_count = 0
            for partial_param in partial_param_str_list:
                if concat_with_previous:
                    param_str_list[-1] += ',' + partial_param
                else:
                    param_str_list.append(partial_param)
                for char in partial_param:
                    if char == '(' or char == '[' or char == '{':
                        open_group_count += 1
                    elif char == ')' or char == ']' or char == '}':
                        open_group_count -= 1
                concat_with_previous = open_group_count != 0

            param_list = [eval(param_str) for param_str in param_str_list]
            for answer_group_index, answer_group in enumerate(answer_groups):
                rule_specs = answer_group.rule_specs
                for rule_spec_index, rule_spec in enumerate(rule_specs):
                    if rule_spec.rule_type != rule_type:
                        continue
                    if unordered_lists_equal(
                            param_list, rule_spec.inputs.values()):
                        return (answer_group_index, rule_spec_index)

        return (None, None)

    @classmethod
    def _infer_classification_categorization(cls, rule_str):
        # At this point, no classification was possible. Thus, only soft, hard,
        # and default classifications are possible.
        fuzzy_rule_type = 'FuzzyMatches'
        if rule_str == cls._DEFAULT_RULESPEC_STR:
            return exp_domain.DEFAULT_OUTCOME_CLASSIFICATION
        elif rule_str == fuzzy_rule_type:
            return exp_domain.TRAINING_DATA_CLASSIFICATION
        else:
            return exp_domain.EXPLICIT_CLASSIFICATION

    @classmethod
    def _get_plaintext(cls, str_value):
        # TODO(bhenning): Convert HTML to plaintext (should just involve
        # stripping <p> tags).
        if '<' in str_value or '>' in str_value:
            return None
        return str_value

    @classmethod
    def _cb_reconstitute_code_evaluation(
            cls, interaction, rule_spec, rule_str, answer_str):
        # The Jinja representation for CodeEvaluation answer strings is:
        #   {{answer.code}}

        rule_types_without_output = [
            'CodeContains', 'CodeDoesNotContain', 'ResultsInError'
        ]
        # NOTE: Not all of CodeEvaluation can be reconstituted. Evaluation,
        # error, and output (with one rule_type exception) cannot be recovered
        # without actually running the code. For this reason, OutputEquals,
        # CodeContains, CodeDoesNotContain, and ResultsInError can only be
        # partially recovered. The missing values will be empty strings as
        # special sentinel values. Empty strings must be checked in conjunction
        # with the session_id to determine whether the empty string is the
        # special sentinel value.

        if rule_spec.rule_type == 'OutputEquals':
            code_output = cls._get_plaintext(rule_spec.inputs['x'])
            code = cls._get_plaintext(answer_str)
            if not code:
                return (None, 'Failed to recover code: %s' % answer_str)
            code_evaluation_dict = {
                'code': code,
                'output': code_output,
                'evaluation': '',
                'error': ''
            }
            return (
                objects.CodeEvaluation.normalize(code_evaluation_dict), None)
        elif rule_spec.rule_type in rule_types_without_output:
            code = cls._get_plaintext(answer_str)
            if not code:
                return (None, 'Failed to recover code: %s' % answer_str)
            code_evaluation_dict = {
                'code': code,
                'output': '',
                'evaluation': '',
                'error': ''
            }
            return (
                objects.CodeEvaluation.normalize(code_evaluation_dict), None)
        return (
            None,
            'Cannot reconstitute a CodeEvaluation object without OutputEquals, '
            'CodeContains, CodeDoesNotContain, or ResultsInError rules.')

    @classmethod
    def _cb_reconstitute_continue(
            cls, interaction, rule_spec, rule_str, answer_str):
        # The Jinja representation for CodeEvaluation answer strings is blank.
        if not rule_spec and not answer_str and (
                rule_str == cls._DEFAULT_RULESPEC_STR):
            # There is no answer for 'Continue' interactions.
            return (None, None)
        return (
            None,
            'Expected Continue submissions to only be default rules: %s'
            % rule_str)

    @classmethod
    def _cb_reconstitute_end_exploration(
            cls, interaction, rule_spec, rule_str, answer_str):
        return (
            None,
            'There should be no answers submitted for the end exploration.')

    @classmethod
    def _cb_reconstitute_graph_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        # pylint: disable=line-too-long
        # The Jinja representation for Graph answer strings is:
        #   ({% for vertex in answer.vertices -%}
        #     {% if answer.isLabeled -%}{{vertex.label}}{% else -%}{{loop.index}}{% endif -%}
        #     {% if not loop.last -%},{% endif -%}
        #   {% endfor -%})
        #   [{% for edge in answer.edges -%}
        #     ({{edge.src}},{{edge.dst}}){% if not loop.last -%},{% endif -%}
        #   {% endfor -%}]

        # This answer type is not being reconsituted. 'HasGraphProperty' has
        # never had an answer submitted for it. 'IsIsomorphicTo' has had 5
        # answers submitted for it, 4 of which are too long to actually
        # reconsititute because the rule_spec_str was cut off in the key name.
        # That leaves 1 lonely graph answer to reconstitute; we're dropping it
        # in favor of avoiding the time needed to build and test the
        # reconstitution of the graph object.
        return (None, 'Unsupported answer type: \'%s\' for answer \'%s\'' % (
            rule_str, answer_str))

    @classmethod
    def _cb_reconstitute_image_click_input(cls, interaction, rule_spec,
                                           rule_str, answer_str):
        # pylint: disable=line-too-long
        # The Jinja representation for ClickOnImage answer strings is:
        #   ({{'%0.3f' | format(answer.clickPosition[0]|float)}}, {{'%0.3f'|format(answer.clickPosition[1]|float)}})
        if rule_spec.rule_type == 'IsInRegion':
            # Extract the region clicked on from the rule string.
            region_name = rule_str[len(rule_spec.rule_type) + 1:-1]

            # Match the pattern: '(real, real)' to extract the coordinates.
            pattern = re.compile(
                r'\((?P<x>\d+\.?\d*), (?P<y>\d+\.?\d*)\)')
            match = pattern.match(answer_str)
            if not match:
                return (
                    None,
                    'Bad answer string in ImageClickInput IsInRegion rule.')
            click_on_image_dict = {
                'clickPosition': [
                    float(match.group('x')), float(match.group('y'))
                ],
                'clickedRegions': [region_name]
            }
            return (objects.ClickOnImage.normalize(click_on_image_dict), None)
        return (
            None,
            'Cannot reconstitute ImageClickInput object without an IsInRegion '
            'rule.')

    @classmethod
    def _cb_reconstitute_interactive_map(
            cls, interaction, rule_spec, rule_str, answer_str):
        # pylint: disable=line-too-long
        # The Jinja representation for CoordTwoDim answer strings is:
        #   ({{'%0.6f' | format(answer[0]|float)}}, {{'%0.6f'|format(answer[1]|float)}})
        supported_rule_types = ['Within', 'NotWithin']
        if rule_spec.rule_type not in supported_rule_types:
            return (
                None,
                'Unsupported rule type encountered while attempting to '
                'reconstitute CoordTwoDim object: %s' % rule_spec.rule_type)

        # Match the pattern: '(real, real)' to extract the coordinates.
        pattern = re.compile(
            r'\((?P<x>-?\d+\.?\d*), (?P<y>-?\d+\.?\d*)\)')
        match = pattern.match(answer_str)
        if not match:
            return (
                None, 'Bad answer string in InteractiveMap %s rule.' % (
                    rule_spec.rule_type))
        coord_two_dim_list = [
            float(match.group('x')), float(match.group('y'))
        ]
        return (objects.CoordTwoDim.normalize(coord_two_dim_list), None)

    @classmethod
    def _cb_reconstitute_item_selection_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        # The Jinja representation for SetOfHtmlString answer strings is:
        #   {{ answer }}
        supported_rule_types = [
            'Equals', 'ContainsAtLeastOneOf', 'DoesNotContainAtLeastOneOf'
        ]
        if rule_spec.rule_type in supported_rule_types:
            option_list = eval(answer_str)
            if not isinstance(option_list, list):
                return (
                    None,
                    'Bad answer string in ItemSelectionInput Equals rule.')
            return (objects.SetOfHtmlString.normalize(option_list), None)
        return (
            None,
            'Cannot reconstitute ItemSelectionInput object without an Equals '
            'rule.')

    @classmethod
    def _cb_reconstitute_logic_proof(
            cls, interaction, rule_spec, rule_str, answer_str):
        if rule_spec.rule_type == 'Correct':
            # The Jinja representation of the answer is:
            #   {{answer.proof_string}}

            # Because the rule implies the proof was correct, half of the
            # CheckedProof structure does not need to be saved. The remaining
            # structure consists of three strings: assumptions_string,
            # target_string, and proof_string. The latter is already available
            # as the answer_str.
            if not answer_str:
                return (
                    None,
                    'Failed to recover CheckedProof answer: %s' % answer_str)

            # assumptions_string and target_string come from the assumptions and
            # results customized to this particular LogicProof instance.
            question_details = (
                interaction.customization_args['question']['value'])
            assumptions = question_details['assumptions']
            results = question_details['results']

            expressions = []
            top_types = []
            for assumption in assumptions:
                expressions.append(assumption)
                top_types.append('boolean')
            expressions.append(results[0])
            top_types.append('boolean')
            operators = AnswerMigrationJob._BASE_STUDENT_LANGUAGE['operators']

            if len(assumptions) <= 1:
                assumptions_string = (
                    AnswerMigrationJob._display_expression_array(
                        assumptions, operators))
            else:
                assumptions_string = '%s and %s' % (
                    AnswerMigrationJob._display_expression_array(
                        assumptions[0:-1], operators),
                    AnswerMigrationJob._display_expression_helper(
                        assumptions[-1], operators, 0))

            target_string = AnswerMigrationJob._display_expression_helper(
                results[0], operators, 0)

            return (objects.CheckedProof.normalize({
                'assumptions_string': assumptions_string,
                'target_string': target_string,
                'proof_string': answer_str,
                'correct': True
            }), None)
        return (
            None,
            'Cannot reconstitute CheckedProof object without a Correct rule.')

    @classmethod
    def _cb_reconstitute_math_expression_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        if rule_spec.rule_type == 'IsMathematicallyEquivalentTo':
            math_expression_dict = eval(answer_str)
            if not isinstance(math_expression_dict, dict):
                return (
                    None,
                    'Bad answer string in MathExpressionInput '
                    'IsMathematicallyEquivalentTo rule.')
            return (
                objects.MathExpression.normalize(math_expression_dict), None)
        return (
            None,
            'Cannot reconstitute MathExpressionInput object without an '
            'IsMathematicallyEquivalentTo rule.')

    @classmethod
    def _cb_reconstitute_multiple_choice_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        # The Jinja representation for NonnegativeInt answer strings is:
        #   {{ choices[answer|int] }}
        if rule_spec.rule_type == 'Equals':
            # Extract the clicked index from the rule string.
            clicked_index = int(rule_str[len(rule_spec.rule_type) + 1:-1])
            customization_args = interaction.customization_args
            choices = customization_args['choices']['value']
            if answer_str != choices[clicked_index]:
                return (
                    None,
                    'Clicked index %d and submitted answer \'%s\' does not '
                    'match corresponding choice in the exploration: \'%s\'' % (
                        clicked_index, answer_str, choices[clicked_index]))
            return (objects.NonnegativeInt.normalize(clicked_index), None)
        return (
            None,
            'Cannot reconstitute MultipleChoiceInput object without an Equals '
            'rule.')

    @classmethod
    def _cb_reconstitute_music_notes_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        # The format of serialized answers is based on the following Jinja:
        #   {% if (answer | length) == 0 -%}
        #     No answer given.
        #   {% else -%}
        #     [{% for note in answer -%}
        #       {% for prop in note -%}
        #         {% if prop == 'readableNoteName' %}{{note[prop]}}{% endif -%}
        #       {% endfor -%}
        #       {% if not loop.last -%},{% endif -%}
        #     {% endfor -%}]
        #   {% endif -%}
        supported_rule_types = [
            'Equals', 'IsLongerThan', 'HasLengthInclusivelyBetween',
            'IsEqualToExceptFor', 'IsTranspositionOf',
            'IsTranspositionOfExceptFor'
        ]
        if rule_spec.rule_type not in supported_rule_types:
            return (
                None,
                'Unsupported rule type encountered while attempting to '
                'reconstitute MusicPhrase object: %s' % rule_spec.rule_type)
        answer_str = answer_str.rstrip()
        if answer_str == 'No answer given.':
            return (objects.MusicPhrase.normalize([]), None)
        if answer_str[0] != '[' or answer_str[-1] != ']' or ' ' in answer_str:
            return (None, 'Invalid music note answer string: %s' % answer_str)
        note_list_str = answer_str[1:-1]
        note_list = note_list_str.split(',')
        for note_str in note_list:
            if note_str not in AnswerMigrationJob._EXPECTED_NOTE_TYPES:
                return (
                    None,
                    'Invalid music note answer string (bad note: %s): %s' % (
                        note_str, answer_str))
        return (objects.MusicPhrase.normalize([{
            'readableNoteName': note_str,
            'noteDuration': {
                'num': AnswerMigrationJob._NOTE_DURATION_FRACTION_PART,
                'den': AnswerMigrationJob._NOTE_DURATION_FRACTION_PART
            }
        } for note_str in note_list]), None)

    @classmethod
    def _cb_reconstitute_numeric_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        supported_rule_types = [
            'Equals', 'IsLessThan', 'IsGreaterThan', 'IsLessThanOrEqualTo',
            'IsGreaterThanOrEqualTo', 'IsInclusivelyBetween',
            'IsWithinTolerance'
        ]
        if rule_spec.rule_type not in supported_rule_types:
            return (
                None,
                'Unsupported rule type encountered while attempting to '
                'reconstitute NumericInput object: %s' % rule_spec.rule_type)
        input_value = float(cls._get_plaintext(answer_str))
        return (objects.Real.normalize(input_value), None)

    @classmethod
    def _cb_reconstitute_pencil_code_editor(
            cls, interaction, rule_spec, rule_str, answer_str):
        if rule_spec.rule_type == 'OutputEquals':
            # Luckily, Pencil Code answers stored the actual dict rather than
            # just the code; it's easier to reconstitute.
            code_evaluation_dict = eval(cls._get_plaintext(answer_str))
            if not isinstance(code_evaluation_dict, dict):
                return (None, 'Failed to recover code: %s' % answer_str)
            return (
                objects.CodeEvaluation.normalize(code_evaluation_dict), None)
        return (
            None,
            'Cannot reconstitute a CodeEvaluation object without an '
            'OutputEquals rule.')

    @classmethod
    def _cb_reconstitute_set_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        supported_rule_types = [
            'Equals', 'IsSubsetOf', 'IsSupersetOf', 'HasElementsIn',
            'HasElementsNotIn', 'OmitsElementsIn', 'IsDisjointFrom'
        ]
        if rule_spec.rule_type not in supported_rule_types:
            return (
                None,
                'Unsupported rule type encountered while attempting to '
                'reconstitute SetInput object: %s' % rule_spec.rule_type)

        unicode_string_list = eval(cls._get_plaintext(answer_str))
        if not isinstance(unicode_string_list, list):
            return (None, 'Failed to recover code: %s' % answer_str)
        return (
            objects.SetOfUnicodeString.normalize(unicode_string_list), None)

    @classmethod
    def _cb_reconstitute_text_input(
            cls, interaction, rule_spec, rule_str, answer_str):
        supported_rule_types = [
            'Equals', 'CaseSensitiveEquals', 'StartsWith', 'Contains',
            'FuzzyEquals'
        ]
        if rule_spec.rule_type not in supported_rule_types:
            return (
                None,
                'Unsupported rule type encountered while attempting to '
                'reconstitute TextInput object: %s' % rule_spec.rule_type)
        input_value = cls._get_plaintext(answer_str)
        return (objects.NormalizedString.normalize(input_value), None)

    @classmethod
    def _reconstitute_answer_object(
            cls, state, rule_spec, rule_str, answer_str):
        interaction_id = state.interaction.id
        if interaction_id in cls._RECONSTITUTION_FUNCTION_MAP:
            # Check for default outcome.
            if (interaction_id != 'Continue'
                    and not rule_spec
                    and rule_str == cls._DEFAULT_RULESPEC_STR):
                return (None, None)
            reconstitute = getattr(
                cls, cls._RECONSTITUTION_FUNCTION_MAP[interaction_id])
            return reconstitute(
                state.interaction, rule_spec, rule_str, answer_str)
        return (
            None,
            'Cannot reconstitute unsupported interaction ID: %s' %
            interaction_id)

    @classmethod
    def entity_classes_to_map_over(cls):
        return [stats_models.StateRuleAnswerLogModel]

    @staticmethod
    def map(item):
        # TODO(bhenning): Throw errors for all major points of failure.

        # TODO(bhenning): Reduce on exp id and state name to reduce exp loads.

        # Cannot unpack the item ID with a simple split, since the rule_str
        # component can contains periods. The ID is guaranteed to always
        # contain 4 parts: exploration ID, state name, handler name, and
        # rule_str.
        item_id = item.id
        period_idx = item_id.index('.')
        exp_id = item_id[:period_idx]

        item_id = item_id[period_idx+1:]
        handler_period_idx = item_id.index('submit') - 1
        state_name = item_id[:handler_period_idx]

        item_id = item_id[handler_period_idx+1:]
        period_idx = item_id.index('.')
        handler_name = item_id[:period_idx]

        item_id = item_id[period_idx+1:]
        rule_str = item_id

        # The exploration and state name are needed in the new data model and
        # are also needed to cross reference the answer. Since the answer is
        # not associated with a particular version, a search needs to be
        # conducted to find which version of the exploration is associated with
        # the given answer.
        if 'submit' not in item.id or handler_name != 'submit':
            yield (
                AnswerMigrationJob._ERROR_KEY,
                'Encountered submitted answer without the standard \'submit\' '
                'handler: %s' % item.id)

        # One major point of failure is the exploration not existing.
        # Another major point of failure comes from the time matching. Since
        # one entity in StateRuleAnswerLogModel represents many different
        # answers, all answers are being matched to a single exploration even
        # though each answer may have been submitted to a different exploration
        # version. This may cause significant migration issues and will be
        # tricky to work around.
        exploration = (
            AnswerMigrationJob._find_exploration_immediately_before_timestamp(
                exp_id, item.created_on))

        # Another point of failure is the state not matching due to an
        # incorrect exploration version selection.
        state = exploration.states[state_name]

        classification_categorization = (
            AnswerMigrationJob._infer_classification_categorization(rule_str))

        # Fuzzy rules are not supported by the migration job. No fuzzy rules
        # should have been submitted in production, so all existing rules are
        # being ignored.
        if classification_categorization == (
                exp_domain.TRAINING_DATA_CLASSIFICATION):
            return

        # Unfortunately, the answer_group_index and rule_spec_index may be
        # wrong for soft rules, since previously there was no way of
        # differentiating between which soft rule was selected. This problem is
        # also revealed for RuleSpecs which produce the same rule_spec_str.
        (answer_group_index, rule_spec_index) = (
            AnswerMigrationJob._infer_which_answer_group_and_rule_match_answer(
                state, rule_str))

        # Major point of failure: answer_group_index or rule_spec_index may
        # return none when it's not a default result.
        if answer_group_index is None or rule_spec_index is None:
            yield (
                AnswerMigrationJob._ERROR_KEY,
                'Failed to match rule string: \'%s\' for answer \'%s\'' % (
                    rule_str, item.id))
            return

        answer_groups = state.interaction.answer_groups
        if answer_group_index != len(answer_groups):
            answer_group = answer_groups[answer_group_index]
            rule_spec = answer_group.rule_specs[rule_spec_index]
        else:
            # The answer is matched with the default outcome.
            answer_group = None
            rule_spec = None

        # These are values which cannot be reconstituted; use special sentinel
        # values for them, instead.
        session_id = stats_domain.MIGRATED_STATE_ANSWER_SESSION_ID
        time_spent_in_sec = (
            stats_domain.MIGRATED_STATE_ANSWER_TIME_SPENT_IN_SEC)

        # Params were, unfortunately, never stored. They cannot be trivially
        # recovered.
        params = []

        # A note on frequency: the resolved answer will simply be duplicated in
        # the new data store to replicate frequency. This is not 100% accurate
        # since each answer may have been submitted at different times and,
        # thus, for different versions of the exploration. This information is
        # practically impossible to recover, so this strategy is considered
        # adequate.
        for answer_str, answer_frequency in item.answers.iteritems():
            # Major point of failure is if answer returns None; the error
            # variable will contain why the reconstitution failed.
            (answer, error) = AnswerMigrationJob._reconstitute_answer_object(
                state, rule_spec, rule_str, answer_str)

            if error:
                yield (AnswerMigrationJob._ERROR_KEY, error)
                continue

            for _ in xrange(answer_frequency):
                stats_services.record_answer(
                    exp_id, exploration.version, state_name, answer_group_index,
                    rule_spec_index, classification_categorization, session_id,
                    time_spent_in_sec, params, answer, rule_spec_str=rule_str,
                    answer_str=answer_str)

    @staticmethod
    def reduce(key, stringified_values):
        # pylint: disable=unused-argument
        for value in stringified_values:
            yield value

    # Following are helpers and constants related to reconstituting the
    # CheckedProof object.

    @classmethod
    def _display_expression_helper(
            cls, expression, operators, desirability_of_brackets):
        """From extensions/interactions/LogicProof/static/js/shared.js"""

        desirability_of_brackets_below = (
            2 if (
                expression['top_kind_name'] == 'binary_connective' or
                expression['top_kind_name'] == 'binary_relation' or
                expression['top_kind_name'] == 'binary_function')
            else 1 if (
                expression['top_kind_name'] == 'unary_connective' or
                expression['top_kind_name'] == 'quantifier')
            else 0)
        processed_arguments = []
        processed_dummies = []
        for argument in expression['arguments']:
            processed_arguments.append(
                AnswerMigrationJob._display_expression_helper(
                    argument, operators, desirability_of_brackets_below))
        for dummy in expression['dummies']:
            processed_dummies.append(
                AnswerMigrationJob._display_expression_helper(
                    dummy, operators, desirability_of_brackets_below))
        symbol = (
            expression['top_operator_name']
            if expression['top_operator_name'] not in operators
            else expression['top_operator_name']
            if 'symbols' not in operators[expression['top_operator_name']]
            else operators[expression['top_operator_name']]['symbols'][0])

        formatted_result = None
        if (expression['top_kind_name'] == 'binary_connective' or
                expression['top_kind_name'] == 'binary_relation' or
                expression['top_kind_name'] == 'binary_function'):
            formatted_result = (
                '(%s)' % processed_arguments.join(symbol)
                if desirability_of_brackets > 0
                else processed_arguments.join(symbol))
        elif expression['top_kind_name'] == 'unary_connective':
            output = '%s%s' % (symbol, processed_arguments[0])
            formatted_result = (
                '(%s)' % output if desirability_of_brackets == 2 else output)
        elif expression['top_kind_name'] == 'quantifier':
            output = '%s%s.%s' % (
                symbol, processed_dummies[0], processed_arguments[0])
            formatted_result = (
                '(%s)' % output if desirability_of_brackets == 2 else output)
        elif expression['top_kind_name'] == 'bounded_quantifier':
            output = '%s%s.%s' % (
                symbol, processed_arguments[0], processed_arguments[1])
            formatted_result = (
                '(%s)' % output if desirability_of_brackets == 2 else output)
        elif (expression['top_kind_name'] == 'prefix_relation'
              or expression['top_kind_name'] == 'prefix_function'):
            formatted_result = (
                '%s(%s)' % (symbol, processed_arguments.join(',')))
        elif expression['top_kind_name'] == 'ranged_function':
            formatted_result = '%s{%s | %s}' % (
                symbol, processed_arguments[0], processed_arguments[1])
        elif (expression['top_kind_name'] == 'atom'
              or expression['top_kind_name'] == 'constant'
              or expression['top_kind_name'] == 'variable'):
            formatted_result = symbol
        else:
            raise Exception('Unknown kind %s sent to displayExpression()' % (
                expression['top_kind_name']))
        return formatted_result

    @classmethod
    def _display_expression_array(cls, expression_array, operators):
        """From extensions/interactions/LogicProof/static/js/shared.js"""

        return ', '.join([
            cls._display_expression_helper(expression, operators, 0)
            for expression in expression_array])

    # These are from extensions/interactions/LogicProof/static/js/data.js
    _SINGLE_BOOLEAN = {
        'type': 'boolean',
        'arbitrarily_many': False
    }
    _SINGLE_ELEMENT = {
        'type': 'element',
        'arbitrarily_many': False
    }
    _BASE_STUDENT_LANGUAGE = {
        'types': {
            'boolean': {
                'quantifiable': False
            },
            'element': {
                'quantifiable': True
            }
        },
        'kinds': {
            'binary_connective': {
                'display': [{
                    'format': 'argument_index',
                    'content': 0
                }, {
                    'format': 'name'
                }, {
                    'format': 'argument_index',
                    'content': 1
                }]
            },
            'unary_connective': {
                'matchable': False,
                'display': [{
                    'format': 'name'
                }, {
                    'format': 'argument_index',
                    'content': 0
                }]
            },
            'quantifier': {
                'matchable': False,
                'display': [{
                    'format': 'name'
                }, {
                    'format': 'dummy_index',
                    'content': 0
                }, {
                    'format': 'string',
                    'content': '.'
                }, {
                    'format': 'argument_index',
                    'content': 0
                }]
            },
            'binary_function': {
                'matchable': False,
                'display': [{
                    'format': 'argument_index',
                    'content': 0
                }, {
                    'format': 'name'
                }, {
                    'format': 'argument_index',
                    'content': 1
                }],
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }]
            },
            'prefix_function': {
                'matchable': False,
                'typing': [{
                    'arguments': [{
                        'type': 'element',
                        'arbitrarily_many': True
                    }],
                    'dummies': [],
                    'output': 'element'
                }, {
                    'arguments': [{
                        'type': 'element',
                        'arbitrarily_many': True
                    }],
                    'dummies': [],
                    'output': 'boolean'
                }]
            },
            'constant': {
                'matchable': False,
                'display': [{
                    'format': 'name'
                }],
                'typing': [{
                    'arguments': [],
                    'dummies': [],
                    'output': 'element'
                }]
            },
            'variable': {
                'matchable': True,
                'display': [{
                    'format': 'name'
                }],
                'typing': [{
                    'arguments': [],
                    'dummies': [],
                    'output': 'element'
                }, {
                    'arguments': [],
                    'dummies': [],
                    'output': 'boolean'
                }]
            }
        },
        'operators': {
            'and': {
                'kind': 'binary_connective',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN, _SINGLE_BOOLEAN],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': [u'\u2227']
            },
            'or': {
                'kind': 'binary_connective',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN, _SINGLE_BOOLEAN],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': [u'\u2228']
            },
            'implies': {
                'kind': 'binary_connective',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN, _SINGLE_BOOLEAN],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['=>']
            },
            'iff': {
                'kind': 'binary_connective',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN, _SINGLE_BOOLEAN],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['<=>']
            },
            'not': {
                'kind': 'unary_connective',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['~']
            },
            'for_all': {
                'kind': 'quantifier',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN],
                    'dummies': [_SINGLE_ELEMENT],
                    'output': 'boolean'
                }],
                'symbols': [u'\u2200', '.']
            },
            'exists': {
                'kind': 'quantifier',
                'typing': [{
                    'arguments': [_SINGLE_BOOLEAN],
                    'dummies': [_SINGLE_ELEMENT],
                    'output': 'boolean'
                }],
                'symbols': [u'\u2203', '.']
            },
            'equals': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['=']
            },
            'not_equals': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['!=']
            },
            'less_than': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['<']
            },
            'greater_than': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['>']
            },
            'less_than_or_equals': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['<=']
            },
            'greater_than_or_equals': {
                'kind': 'binary_relation',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'boolean'
                }],
                'symbols': ['>=']
            },
            'addition': {
                'kind': 'binary_function',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }],
                'symbols': ['+']
            },
            'subtraction': {
                'kind': 'binary_function',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }],
                'symbols': ['-']
            },
            'multiplication': {
                'kind': 'binary_function',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }],
                'symbols': ['*']
            },
            'division': {
                'kind': 'binary_function',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }],
                'symbols': ['/']
            },
            'exponentiation': {
                'kind': 'binary_function',
                'typing': [{
                    'arguments': [_SINGLE_ELEMENT, _SINGLE_ELEMENT],
                    'dummies': [],
                    'output': 'element'
                }],
                'symbols': ['^']
            }
        }
    }
