ELIGIBLE_BATCH_SQL = """
WITH ranked AS (
    SELECT
        s.id AS subscription_id,
        s.user_id,
        COALESCE(s.remnawave_uuid, u.remnawave_uuid)::text AS remnawave_uuid,
        s.end_date,
        row_number() OVER (
            PARTITION BY COALESCE(s.remnawave_uuid, u.remnawave_uuid)
            ORDER BY s.end_date DESC NULLS LAST, s.id DESC
        ) AS candidate_rank
    FROM public.subscriptions s
    JOIN public.users u ON u.id = s.user_id
    LEFT JOIN public.tariffs tf ON tf.id = s.tariff_id
    LEFT JOIN grace_bridge.access_state ga ON ga.subscription_id = s.id
    WHERE lower(s.status) = 'expired'
      AND s.end_date <= now()
      AND s.is_trial IS FALSE
      AND lower(u.status) = 'active'
      AND u.has_had_paid_subscription IS TRUE
      AND COALESCE(u.restriction_subscription, false) IS FALSE
      AND COALESCE(tf.is_daily, false) IS FALSE
      AND COALESCE(s.remnawave_uuid, u.remnawave_uuid) IS NOT NULL
      AND COALESCE(ga.state, 'inactive') = 'inactive'
      AND COALESCE(s.remnawave_uuid, u.remnawave_uuid)::text > $1
      AND ($2::uuid IS NULL OR COALESCE(s.remnawave_uuid, u.remnawave_uuid)::uuid = $2)
      AND NOT EXISTS (
          SELECT 1 FROM public.subscriptions active_sub
          WHERE active_sub.user_id = s.user_id
            AND active_sub.id <> s.id
            AND lower(active_sub.status) IN ('active', 'trial')
            AND active_sub.end_date > now()
      )
      AND EXISTS (
          SELECT 1 FROM public.transactions tr
          WHERE tr.user_id = s.user_id
            AND tr.is_completed IS TRUE
            AND tr.amount_kopeks <> 0
            AND tr.type = 'subscription_payment'
      )
)
SELECT subscription_id, user_id, remnawave_uuid, end_date
FROM ranked
WHERE candidate_rank = 1
ORDER BY remnawave_uuid, subscription_id
LIMIT $3
"""

ELIGIBLE_BY_ID_SQL = """
SELECT
    s.id AS subscription_id,
    s.user_id,
    COALESCE(s.remnawave_uuid, u.remnawave_uuid)::text AS remnawave_uuid,
    s.end_date
FROM public.subscriptions s
JOIN public.users u ON u.id = s.user_id
LEFT JOIN public.tariffs tf ON tf.id = s.tariff_id
WHERE s.id = $1
  AND lower(s.status) = 'expired'
  AND s.end_date <= now()
  AND s.is_trial IS FALSE
  AND lower(u.status) = 'active'
  AND u.has_had_paid_subscription IS TRUE
  AND COALESCE(u.restriction_subscription, false) IS FALSE
  AND COALESCE(tf.is_daily, false) IS FALSE
  AND COALESCE(s.remnawave_uuid, u.remnawave_uuid) IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.subscriptions active_sub
      WHERE active_sub.user_id = s.user_id
        AND active_sub.id <> s.id
        AND lower(active_sub.status) IN ('active', 'trial')
        AND active_sub.end_date > now()
  )
  AND EXISTS (
      SELECT 1 FROM public.transactions tr
      WHERE tr.user_id = s.user_id
        AND tr.is_completed IS TRUE
        AND tr.amount_kopeks <> 0
        AND tr.type = 'subscription_payment'
  )
"""
