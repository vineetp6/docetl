import concurrent
import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Tuple

from rich.console import Console
from rich.table import Table

from motion.optimizers.map_optimizer.evaluator import Evaluator
from motion.optimizers.map_optimizer.plan_generators import PlanGenerator
from motion.optimizers.map_optimizer.prompt_generators import PromptGenerator
from motion.optimizers.map_optimizer.utils import select_evaluation_samples
from motion.optimizers.utils import LLMClient


class MapOptimizer:
    """
    A class for optimizing map operations in data processing pipelines.

    This optimizer analyzes the input operation configuration and data,
    and generates optimized plans for executing the operation. It can
    create plans for chunking, metadata extraction, gleaning, chain
    decomposition, and parallel execution.

    Attributes:
        config (Dict[str, Any]): The configuration dictionary for the optimizer.
        console (Console): A Rich console object for pretty printing.
        llm_client (LLMClient): A client for interacting with a language model.
        _run_operation (Callable): A function to execute operations.
        max_threads (int): The maximum number of threads to use for parallel execution.
        timeout (int): The timeout in seconds for operation execution.

    """

    def __init__(
        self,
        config: Dict[str, Any],
        console: Console,
        llm_client: LLMClient,
        max_threads: int,
        run_operation: Callable,
        timeout: int = 10,
        is_filter: bool = False,
    ):
        """
        Initialize the MapOptimizer.

        Args:
            config (Dict[str, Any]): The configuration dictionary for the optimizer.
            console (Console): A Rich console object for pretty printing.
            llm_client (LLMClient): A client for interacting with a language model.
            max_threads (int): The maximum number of threads to use for parallel execution.
            run_operation (Callable): A function to execute operations.
            timeout (int, optional): The timeout in seconds for operation execution. Defaults to 10.
            is_filter (bool, optional): If True, the operation is a filter operation. Defaults to False.
        """
        self.config = config
        self.console = console
        self.llm_client = llm_client
        self._run_operation = run_operation
        self.max_threads = max_threads
        self.timeout = timeout
        self._num_plans_to_evaluate_in_parallel = 5
        self.is_filter = is_filter

        self.plan_generator = PlanGenerator(
            llm_client, console, config, run_operation, max_threads, is_filter
        )
        self.evaluator = Evaluator(
            llm_client,
            console,
            run_operation,
            timeout,
            self._num_plans_to_evaluate_in_parallel,
            is_filter,
        )
        self.prompt_generator = PromptGenerator(
            llm_client, console, config, max_threads, is_filter
        )

    def optimize(
        self, op_config: Dict[str, Any], input_data: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
        """
        Optimize the given operation configuration for the input data.
        This method analyzes the operation and input data, generates various
        optimization plans, evaluates them, and returns the best plan along
        with its output. A key part of this process is creating a custom
        validator prompt for evaluation. The validator prompt is generated
        based on the specific task, input data, and output data. It serves
        as a critical tool for assessing the quality and correctness of
        each optimization plan's output. This custom prompt ensures that
        the evaluation is tailored to the unique requirements and nuances
        of the given operation. The types of optimization plans include:

        1. Improved Prompt Plan: Enhances the original prompt based on evaluation, aiming to improve output quality.

        2. Chunk Size Plan: Splits input data into chunks of different sizes,
           processes each chunk separately, and then combines the results. This
           can improve performance for large inputs.

        3. Gleaning Plans: Implements an iterative refinement process where the
           output is validated and improved over multiple rounds, enhancing accuracy.

        4. Chain Decomposition Plan: Breaks down complex operations into a series
           of simpler sub-operations, potentially improving overall performance
           and interpretability.

        5. Parallel Map Plan: Decomposes the task into subtasks that can be
           executed in parallel, potentially speeding up processing for
           independent operations.

        The method generates these plans, evaluates their performance using
        a custom validator, and selects the best performing plan based on
        output quality and execution time.

        Args:
            op_config (Dict[str, Any]): The configuration of the operation to optimize.
            input_data (List[Dict[str, Any]]): The input data for the operation.

        Returns:
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]: A tuple containing
            the best optimization plan and its output. The plan is a list of
            operation configurations that achieve the best performance.
            The cost is the cost of the optimizer (from possibly synthesizing resolves).

        """
        input_data = copy.deepcopy(input_data)
        # Add id to each input_data
        for i in range(len(input_data)):
            input_data[i]["_map_opt_id"] = str(uuid.uuid4())

        # Execute the original operation on the sample data
        no_change_start = time.time()
        output_data = self._run_operation(op_config, input_data)
        no_change_runtime = time.time() - no_change_start

        # Generate custom validator prompt
        validator_prompt = self.prompt_generator._generate_validator_prompt(
            op_config, input_data, output_data
        )

        # Log the validator prompt
        self.console.log("[bold]Validator Prompt:[/bold]")
        self.console.log(validator_prompt)
        self.console.log("\n")  # Add a newline for better readability

        # Step 2: Use the validator prompt to assess the operation's performance
        assessment = self.evaluator._assess_operation(
            op_config, input_data, output_data, validator_prompt
        )

        # Print out the assessment
        self.console.log(
            f"[bold]Assessment for whether we should improve operation {op_config['name']}:[/bold]"
        )
        self.console.log(json.dumps(assessment, indent=2))
        self.console.log("\n")  # Add a newline for better readability

        # Check if improvement is needed based on the assessment
        if not assessment.get("needs_improvement", True):
            self.console.log(
                f"[green]No improvement needed for operation {op_config['name']}[/green]"
            )
            return [op_config], output_data, self.plan_generator.reduce_optimizer_cost

        # Generate improved prompt plan
        improved_prompt_plan = self.prompt_generator._get_improved_prompt(
            op_config, assessment, input_data
        )

        # Generate chunk size plans
        chunk_size_plans = self.plan_generator._generate_chunk_size_plans(
            op_config, input_data, validator_prompt
        )

        # Generate gleaning plans
        gleaning_plans = self.plan_generator._generate_gleaning_plans(
            op_config, validator_prompt
        )

        # Generate chain decomposition plans
        if not self.is_filter:
            chain_plans = self.plan_generator._generate_chain_plans(
                op_config, input_data
            )
        else:
            chain_plans = {}

        # Generate parallel map plans
        if not self.is_filter:
            parallel_plans = self.plan_generator._generate_parallel_plans(
                op_config, input_data
            )
        else:
            parallel_plans = {}

        # Evaluate all plans
        plans_to_evaluate = {
            "improved_instructions": improved_prompt_plan,
            "no_change": [op_config],
            **chunk_size_plans,
            **gleaning_plans,
            **chain_plans,
            **parallel_plans,
        }

        # Select consistent evaluation samples
        num_evaluations = min(5, len(input_data))
        evaluation_samples = select_evaluation_samples(input_data, num_evaluations)

        results = {}
        plans_list = list(plans_to_evaluate.items())
        for i in range(0, len(plans_list), self._num_plans_to_evaluate_in_parallel):
            batch = plans_list[i : i + self._num_plans_to_evaluate_in_parallel]
            with ThreadPoolExecutor(
                max_workers=self._num_plans_to_evaluate_in_parallel
            ) as executor:
                futures = {
                    executor.submit(
                        self.evaluator._evaluate_plan,
                        plan_name,
                        op_config,
                        plan,
                        copy.deepcopy(evaluation_samples),
                        validator_prompt,
                    ): plan_name
                    for plan_name, plan in batch
                }
                for future in as_completed(futures):
                    plan_name = futures[future]
                    try:
                        score, runtime, output = future.result(timeout=self.timeout)
                        results[plan_name] = (score, runtime, output)
                    except concurrent.futures.TimeoutError:
                        self.console.log(
                            f"[yellow]Plan {plan_name} timed out and will be skipped.[/yellow]"
                        )
                    except Exception as e:
                        # TODO: raise this error if the error is related to a Jinja error
                        self.console.log(
                            f"[red]Error in plan {plan_name}: {str(e)}[/red]"
                        )
                        import traceback

                        print(traceback.format_exc())

        # Add no change plan
        results["no_change"] = (
            results["no_change"][0],
            no_change_runtime,
            results["no_change"][2],
        )

        # Create a table of scores sorted in descending order
        scores = sorted(
            [(score, runtime, plan) for plan, (score, runtime, _) in results.items()],
            reverse=True,
        )

        self.console.log(
            f"\n[bold]Plan Evaluation Results for {op_config['name']} ({op_config['type']}, {len(scores)} plans, {num_evaluations} samples):[/bold]"
        )
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Plan", style="dim")
        table.add_column("Score", justify="right", width=10)
        table.add_column("Runtime", justify="right", width=10)
        table.add_column("Pairwise Wins", justify="right", width=10)

        # Sort results by score in descending order
        sorted_results = sorted(results.items(), key=lambda x: x[1][0], reverse=True)

        # Take the top 6 plans
        top_plans = sorted_results[:6]

        # Include any additional plans that are tied with the last plan
        tail_score = top_plans[-1][1][0] if len(top_plans) == 6 else float("-inf")
        filtered_results = dict(
            top_plans
            + [
                item
                for item in sorted_results[len(top_plans) :]
                if item[1][0] == tail_score
            ]
        )

        # Perform pairwise comparisons on filtered plans
        if len(filtered_results) > 1:
            pairwise_rankings = self.evaluator._pairwise_compare_plans(
                filtered_results, validator_prompt, op_config, evaluation_samples
            )
            best_plan_name = max(pairwise_rankings, key=pairwise_rankings.get)
        else:
            pairwise_rankings = {k: 0 for k in results.keys()}
            best_plan_name = (
                next(iter(filtered_results))
                if filtered_results
                else max(results, key=lambda x: results[x][0])
            )

        for score, runtime, plan in scores:
            table.add_row(
                plan,
                f"{score:.2f}",
                f"{runtime:.2f}s",
                f"{pairwise_rankings.get(plan, 0)}",
            )

        self.console.log(table)
        self.console.log("\n")

        _, _, best_output = results[best_plan_name]
        self.console.log(
            f"[green]Choosing {best_plan_name} for operation {op_config['name']} (Score: {results[best_plan_name][0]:.2f}, Runtime: {results[best_plan_name][1]:.2f}s)[/green]"
        )

        return (
            plans_to_evaluate[best_plan_name],
            best_output,
            self.plan_generator.reduce_optimizer_cost,
        )
