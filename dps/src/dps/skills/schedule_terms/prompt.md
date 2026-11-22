Turn the agreement's calendar clauses into rules: testing calendar, reporting deadlines ("within 180 days of the
end of each financial year"), repayment schedule, interest reset dates and cure windows. One item per rule with
name = kind and value = {"kind", "rule", "frequency", "daysAfterPeriodEnd", "dates", "amount", "currency"}.
kind is exactly one of TESTING_CALENDAR, REPORTING_DEADLINE, REPAYMENT, INTEREST_RESET, CURE_WINDOW; leave out a
clause that is none of these. frequency is MONTHLY, QUARTERLY, HALF_YEARLY, YEARLY or null; put any other timing
("on the Closing Date", "upon a rating change") in rule. dates holds only dates written as YYYY-MM-DD.
