# Ecology cards for actual human review

Human review is **not run**. These six cards are copied without editing from repeat 1 of the full production deck at `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75`. Repeat 2 published identical card content. A valid request or independent-agent review is not human acceptance.

[Full deck, both repeats, sources, provenance and call transcripts](study-ecology-decks.json).

For each card, assess factual accuracy, source grounding, clarity, assumptions and usefulness. Record the reviewer, date and any requested correction before marking a human result.

## Card 1: Quadrat sampling

**Front:** In the synthetic meadow example, five $2\ \text{m}^2$ quadrats contain 3, 5, 4, 6, and 2 daisies. What is the estimated daisy density?

**Back:** The total count is $3+5+4+6+2=20$ daisies, and the total sampled area is $5 \times 2 = 10\ \text{m}^2$. The estimated density is therefore $20/10 = 2$ daisies per $\text{m}^2$.

Relevant supplied source: Population survey.txt, page 3. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

## Card 5: Density estimation and extrapolation

**Front:** In simple mark-recapture, if $M=40$ animals are initially marked, $C=50$ animals are caught later, and $R=10$ of that later catch are marked, what is the estimated population size $N$?

**Back:** Use the simple mark-recapture estimator $N = M C / R$. Substituting the values gives $N = (40)(50)/10 = 200$ animals.

Relevant supplied source: Mark recapture.txt, page 4. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

## Card 6: Density estimation and extrapolation

**Front:** In simple mark-recapture, if no marked animals are recaptured, so $R=0$, what happens to the estimate $N = M C / R$, and what conclusion is not justified?

**Back:** The simple estimate is undefined because it requires division by zero. This does not justify concluding that the population is infinite; it only means the simple mark-recapture calculation cannot be made.

Relevant supplied source: Mark recapture.txt, page 4. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

## Card 7: Sampling variation and systematic bias

**Front:** In simple mark-recapture, where $M$ animals are initially marked, $C$ are caught later, and $R$ of that later catch are marked, why does migration between the two catches bias the estimate $N = M C / R$?

**Back:** It violates the closed-population assumption. If animals enter or leave between marking and recapture, the later catch no longer reflects the original marked fraction, so $R/C$ is systematically biased and $N$ is biased.

Relevant supplied source: Mark recapture.txt, page 4. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

Known qualification concern: the supplied material says migration **can** bias an estimate. This card says it **does** and describes the recapture proportion as systematically biased. Check that the assumptions and target population justify the stronger wording; otherwise request a qualification. The published wording above remains unchanged for review.

## Card 11: Mark-recapture assumptions and biases

**Front:** In simple mark-recapture, why is the ratio $R/C$ used as an estimate of the marked fraction of the population, and what does that imply for estimating $N$?

**Back:** If marked animals have mixed back and capture probabilities are equal, the second catch is a random sample of the population. Thus the fraction marked in the catch, $R/C$, estimates the fraction marked in the whole population, $M/N$. Equating these gives $M/N \approx R/C$, so $N \approx M/(R/C)=MC/R$.

Relevant supplied source: Mark recapture.txt, page 4. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

## Card 14: Correlation and causation

**Front:** What may produce a correlation between rainfall and abundance without rainfall causing abundance?

**Back:** A confounder that may affect both rainfall and abundance, so the two variables are correlated even if rainfall does not directly cause abundance.

Relevant supplied source: Mark recapture.txt, page 4. The full record preserves all actual retrieval provenance.

Human result: ☐ Pass ☐ Needs correction — reviewer/date: __________

## Supplied source excerpts for comparison

**Population survey.txt, page 3:** A quadrat is a fixed-area sample used to estimate density of sessile organisms. Random placement reduces selection bias. In a synthetic meadow, five 2 m^2 quadrats contain 3, 5, 4, 6 and 2 daisies. Total sampled area is 10 m^2, total count 20, estimated density 2 daisies per m^2. Extrapolating to a comparable 150 m^2 meadow gives 300 daisies; this assumes samples represent the meadow. A larger sample reduces sampling variation but does not automatically remove systematic selection bias.

**Mark recapture.txt, page 4:** The simple mark-recapture estimate is N = M*C/R, where M animals were initially marked, C animals were caught later, and R of that later catch were marked. With M=40, C=50, R=10, N=200 animals. Assumptions: closed population between catches, marks retained and recognised, marked animals mix back into population, and equal capture probability. Migration or unequal catchability can bias estimates. If no marked animals are recaptured, division by zero prevents this simple estimate; it is not evidence of an infinite population. Quadrat sampling suits sessile plants; mark-recapture suits mobile animals. Correlation between rainfall and abundance alone does not establish causation because confounders may affect both.
