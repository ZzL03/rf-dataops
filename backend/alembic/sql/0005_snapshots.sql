CREATE FUNCTION rf_frozen_prediction(prediction_uuid uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object('id',p.id,'label',p.label,'scores',p.scores,
        'confidence',p.confidence,'is_mock',p.is_mock,'created_by_job_id',p.created_by_job_id,
        'model',jsonb_build_object('id',m.id,'name',m.name,'version',m.version,
            'adapter',m.adapter,'artifact_sha256',m.artifact_sha256,
            'config',m.config-'artifact_path'))
    FROM predictions p JOIN model_versions m ON p.model_version_id=m.id
    WHERE p.id=prediction_uuid
$$;

CREATE FUNCTION rf_dataset_manifest(version_uuid uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object('schema_version','rf-dataops-manifest-v1',
        'dataset',jsonb_build_object('id',d.id,'name',d.name,'description',d.description,
            'root_key',d.root_key),
        'version',jsonb_build_object('id',v.id,'name',v.version,'published_by',v.published_by,
            'publish_job_id',v.publish_job_id,'created_at',
            to_char(v.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
        'selection',v.selection,'label_policy',v.label_policy,
        'model_version_id',v.model_version_id,'allow_mock_predictions',v.allow_mock_predictions,
        'item_count',(SELECT count(*) FROM dataset_version_items i WHERE i.dataset_version_id=v.id),
        'label_distribution',COALESCE((SELECT jsonb_object_agg(t.frozen_label,t.n)
            FROM (SELECT frozen_label,count(*) n FROM dataset_version_items
                WHERE dataset_version_id=v.id GROUP BY frozen_label) t),'{}'::jsonb),
        'source_counts',COALESCE((SELECT jsonb_object_agg(t.label_source,t.n)
            FROM (SELECT label_source,count(*) n FROM dataset_version_items
                WHERE dataset_version_id=v.id GROUP BY label_source) t),'{}'::jsonb),
        'contains_mock_predictions',COALESCE((SELECT bool_or(label_source='MODEL'
            AND (snapshot->'prediction'->>'is_mock')::boolean) FROM dataset_version_items
            WHERE dataset_version_id=v.id),false),
        'items',COALESCE((SELECT jsonb_agg(snapshot ORDER BY segment_id)
            FROM dataset_version_items WHERE dataset_version_id=v.id),'[]'::jsonb))
    FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=version_uuid
$$;

CREATE FUNCTION rf_guard_dataset_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE j processing_jobs; d datasets; u users; payload jsonb; expected jsonb;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'published history cannot be deleted' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        SELECT * INTO j FROM processing_jobs WHERE id=NEW.publish_job_id;
        SELECT * INTO d FROM datasets WHERE id=NEW.dataset_id;
        SELECT * INTO u FROM users WHERE id=NEW.published_by;
        payload := j.payload->'publication';
        IF NEW.sealed OR NEW.item_count<>0 OR NEW.manifest_text<>'' OR NEW.manifest_sha256<>''
            OR NEW.creation_xid<>pg_current_xact_id()::text
            OR j.job_type IS DISTINCT FROM 'PUBLISH' OR j.status IS DISTINCT FROM 'RUNNING'
            OR j.dataset_id IS DISTINCT FROM NEW.dataset_id OR j.root_key IS DISTINCT FROM d.root_key
            OR NEW.published_by::text IS DISTINCT FROM j.payload->>'publisher_id'
            OR u.role NOT IN ('ADMIN','RESEARCHER') OR NOT u.is_active
            OR NEW.version IS DISTINCT FROM payload->>'version'
            OR NEW.selection IS DISTINCT FROM payload->'selection'
            OR NEW.label_policy IS DISTINCT FROM payload->>'label_policy'
            OR NEW.model_version_id::text IS DISTINCT FROM payload->>'model_version_id'
            OR NEW.allow_mock_predictions IS DISTINCT FROM
                (payload->>'allow_mock_predictions')::boolean THEN
            RAISE EXCEPTION 'invalid publication origin or initial state' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.sealed OR NOT NEW.sealed OR OLD.creation_xid<>pg_current_xact_id()::text
        OR (to_jsonb(NEW)-ARRAY['sealed','manifest_text','manifest_sha256','item_count'])
            IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['sealed','manifest_text','manifest_sha256','item_count']) THEN
        RAISE EXCEPTION 'dataset version is immutable or seal is invalid' USING ERRCODE='23514';
    END IF;
    expected := rf_dataset_manifest(NEW.id);
    IF NEW.item_count<=0 OR NEW.item_count>10000
        OR NEW.item_count IS DISTINCT FROM (expected->>'item_count')::integer
        OR NEW.manifest_text::jsonb IS DISTINCT FROM expected
        OR NEW.manifest_sha256 IS DISTINCT FROM
            encode(sha256(convert_to(NEW.manifest_text,'UTF8')),'hex') THEN
        RAISE EXCEPTION 'manifest does not exactly describe members' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_dataset_version BEFORE INSERT OR UPDATE OR DELETE ON dataset_versions
FOR EACH ROW EXECUTE FUNCTION rf_guard_dataset_version();

CREATE FUNCTION rf_guard_dataset_item() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v dataset_versions; d datasets; s signal_segments; r raw_files; c processing_configs;
    a annotations; p predictions; t review_tasks; frozen jsonb; chosen_label text;
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'dataset members are immutable' USING ERRCODE='23514';
    END IF;
    SELECT * INTO v FROM dataset_versions WHERE id=NEW.dataset_version_id FOR UPDATE;
    IF NOT FOUND OR v.sealed OR v.creation_xid<>pg_current_xact_id()::text THEN
        RAISE EXCEPTION 'cannot append to a sealed or foreign-transaction version'
            USING ERRCODE='23514';
    END IF;
    SELECT * INTO d FROM datasets WHERE id=v.dataset_id;
    SELECT * INTO s FROM signal_segments WHERE id=NEW.segment_id;
    SELECT * INTO r FROM raw_files WHERE id=s.raw_file_id;
    SELECT * INTO c FROM processing_configs WHERE id=s.config_id;
    IF r.root_key IS DISTINCT FROM d.root_key OR r.status IS DISTINCT FROM 'ACTIVE'
        OR NEW.raw_file_id IS DISTINCT FROM s.raw_file_id THEN
        RAISE EXCEPTION 'invalid dataset member source' USING ERRCODE='23514';
    END IF;
    IF NEW.label_source='HUMAN' THEN
        SELECT * INTO a FROM annotations WHERE id=NEW.annotation_id;
        SELECT * INTO t FROM review_tasks WHERE id=a.review_task_id;
        IF NEW.prediction_id IS NOT NULL OR t.segment_id IS DISTINCT FROM s.id
            OR t.current_annotation_id IS DISTINCT FROM a.id THEN
            RAISE EXCEPTION 'annotation is not the selected current human record'
                USING ERRCODE='23514';
        END IF;
        chosen_label := a.label;
    ELSIF NEW.label_source='MODEL' THEN
        SELECT * INTO p FROM predictions WHERE id=NEW.prediction_id;
        SELECT * INTO t FROM review_tasks WHERE segment_id=s.id;
        IF v.label_policy<>'MODEL_ALLOWED' OR NEW.annotation_id IS NOT NULL
            OR p.segment_id IS DISTINCT FROM s.id
            OR p.model_version_id IS DISTINCT FROM v.model_version_id
            OR (p.is_mock AND NOT v.allow_mock_predictions)
            OR t.current_annotation_id IS NOT NULL THEN
            RAISE EXCEPTION 'model policy, version or human-priority rule violated'
                USING ERRCODE='23514';
        END IF;
        chosen_label := p.label;
    ELSE
        RAISE EXCEPTION 'invalid label source' USING ERRCODE='23514';
    END IF;
    IF chosen_label IS DISTINCT FROM NEW.frozen_label THEN
        RAISE EXCEPTION 'frozen label differs from its source' USING ERRCODE='23514';
    END IF;
    IF (jsonb_array_length(v.selection->'raw_file_ids')>0
            AND NOT (v.selection->'raw_file_ids' ? r.id::text))
        OR (jsonb_array_length(v.selection->'segment_ids')>0
            AND NOT (v.selection->'segment_ids' ? s.id::text))
        OR (jsonb_array_length(v.selection->'labels')>0
            AND NOT (v.selection->'labels' ? chosen_label))
        OR ((v.selection->>'review_status') IS NOT NULL AND
            v.selection->>'review_status' IS DISTINCT FROM COALESCE(t.status,'PENDING')) THEN
        RAISE EXCEPTION 'member does not satisfy frozen selection' USING ERRCODE='23514';
    END IF;
    frozen := jsonb_build_object('segment_id',s.id,'source',jsonb_build_object(
        'id',r.id,'root_key',r.root_key,'relative_path',r.relative_path,'sha256',r.sha256,
        'metadata_hash',r.metadata_hash,'byte_size',r.byte_size,'format',r.format,
        'iq_metadata',r.iq_metadata),
        'interval',jsonb_build_object('start_sample',s.start_sample,'sample_count',s.sample_count),
        'processing',jsonb_build_object('id',c.id,'config_hash',c.config_hash,
            'parameters',c.parameters,'reader_version',c.reader_version,
            'pipeline_version',c.pipeline_version),
        'label_source',NEW.label_source,'label',chosen_label,
        'annotation',CASE WHEN NEW.label_source='HUMAN' THEN jsonb_build_object(
            'id',a.id,'version',a.version,'reviewer_id',a.reviewer_id,'action',a.action,
            'comment',a.comment,'prediction_id',a.prediction_id,'created_at',
            to_char(a.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
            ELSE NULL END,
        'prediction',rf_frozen_prediction(CASE WHEN NEW.label_source='HUMAN'
            THEN a.prediction_id ELSE p.id END));
    IF NEW.snapshot IS NOT NULL AND NEW.snapshot<>'{}'::jsonb AND NEW.snapshot<>frozen THEN
        RAISE EXCEPTION 'forged frozen payload' USING ERRCODE='23514';
    END IF;
    NEW.snapshot := frozen;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_dataset_item BEFORE INSERT OR UPDATE OR DELETE ON dataset_version_items
FOR EACH ROW EXECUTE FUNCTION rf_guard_dataset_item();

CREATE FUNCTION rf_require_sealed_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (SELECT sealed FROM dataset_versions WHERE id=NEW.id) THEN
        RAISE EXCEPTION 'unsealed dataset version cannot commit' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER require_sealed_version AFTER INSERT OR UPDATE ON dataset_versions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rf_require_sealed_version();
CREATE TRIGGER protect_datasets BEFORE UPDATE OR DELETE ON datasets
FOR EACH ROW EXECUTE FUNCTION protect_asset_history();
