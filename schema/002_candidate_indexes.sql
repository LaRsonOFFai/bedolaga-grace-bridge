-- This migration intentionally uses CONCURRENTLY and therefore must not run
-- inside a transaction. Index names are project-specific and never replace
-- Bedolaga indexes.
CREATE INDEX CONCURRENTLY IF NOT EXISTS grace_bridge_subscriptions_candidate_idx
    ON public.subscriptions (status, end_date, remnawave_uuid, id)
    WHERE is_trial IS FALSE;

CREATE INDEX CONCURRENTLY IF NOT EXISTS grace_bridge_transactions_paid_idx
    ON public.transactions (user_id)
    WHERE is_completed IS TRUE AND amount_kopeks <> 0 AND type = 'subscription_payment';
