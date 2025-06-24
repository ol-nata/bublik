# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2016-2023 OKTET Labs Ltd. All rights reserved.

from __future__ import annotations

from typing import ClassVar

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.serializers import ModelSerializer

from bublik.core.hash_system import HashedModelSerializer
from bublik.core.meta.categorization import categorize_meta
from bublik.core.shortcuts import serialize
from bublik.core.utils import empty_to_none
from bublik.data.models import (
    Meta,
    MetaResult,
    MetaTest,
    Test,
    TestArgument,
    TestIteration,
    TestIterationRelation,
    TestIterationResult,
)
from bublik.data.serializers.meta import MetaSerializer
from bublik.data.serializers.reference import ReferenceSerializer


__all__ = [
    'MetaResultSerializer',
    'TestArgumentSerializer',
    'TestIterationRelationSerializer',
    'TestIterationResultSerializer',
    'TestIterationSerializer',
    'TestSerializer',
]


class TestSerializer(ModelSerializer):
    class Meta:
        model = Test
        fields = ('id', 'name', 'parent', 'result_type')


class TestArgumentSerializer(HashedModelSerializer):
    class Meta:
        model = TestArgument
        fields = model.hashable
        extra_kwargs: ClassVar[dict] = {'value': {'trim_whitespace': False}}


class TestIterationSerializer(ModelSerializer):
    class Meta:
        model = TestIteration
        fields = ('id', 'test', 'test_arguments', 'hash')


class TestIterationRelationSerializer(ModelSerializer):
    class Meta:
        model = TestIterationRelation
        fields = ('id', 'test_iteration', 'parent_iteration', 'depth')


class TestIterationResultSerializer(ModelSerializer):
    class Meta:
        model = TestIterationResult
        fields = (
            'id',
            'iteration',
            'test_run',
            'parent_package',
            'tin',
            'exec_seqno',
            'start',
            'finish',
        )


class MetaResultSerializer(ModelSerializer):
    meta = MetaSerializer(required=True)
    reference = ReferenceSerializer(required=False, default=None, allow_null=True)

    class Meta:
        model = MetaResult
        fields = ('id', 'meta', 'reference', 'result', 'ref_index', 'serial')
        extra_kwargs: ClassVar[dict] = {
            'ref_index': {'default': None},
            'serial': {'default': 0},
        }

    def to_internal_value(self, data):
        data = empty_to_none(data, ['ref_index'])
        return super().to_internal_value(data)

    def get_or_create(self):
        meta_data = self.validated_data.pop('meta')
        meta_serializer = serialize(MetaSerializer, meta_data)
        meta, created = meta_serializer.get_or_create()
        if created:
            categorize_meta(meta)

        reference = self.validated_data.pop('reference', None)
        if reference:
            reference_serializer = serialize(ReferenceSerializer, reference)
            reference, _ = reference_serializer.get_or_create()

        return MetaResult.objects.get_or_create(
            **self.validated_data,
            meta=meta,
            reference=reference,
        )


class MetaTestSerializer(ModelSerializer):
    comment = serializers.CharField(
        help_text=(
            'This is the comment field representing the value of the meta object, '
            'corresponding to the comment'
        ),
        source='meta.value',
    )

    class Meta:
        model = MetaTest
        fields: ClassVar[tuple[str, ...]] = ('id', 'comment', 'test', 'project', 'serial')
        read_only_fields: ClassVar[tuple[str, ...]] = ('test', 'project', 'serial')

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        data['meta']['type'] = 'comment'
        return data

    def validate_comment(self, comment):
        test = getattr(self.instance, 'test', None) or self.context.get('test')
        project = getattr(self.instance, 'project', None) or self.context.get('project')

        same_comments = self.Meta.model.objects.filter(
            test=test,
            project=project,
            meta__value=comment,
        )
        if self.instance:
            same_comments = same_comments.exclude(id=self.instance.id)

        if same_comments.exists():
            msg = 'A comment with the same content already exists for this test and project'
            raise serializers.ValidationError(msg)

        return comment

    def validate(self, attrs):
        '''
        Update the attributes with the serial number, test, and project.
        '''
        if self.instance:
            return attrs

        # assign the serial number
        latest_serial = (
            MetaTest.objects.filter(
                test=self.context['test'],
                project=self.context['project'],
            )
            .order_by('serial')
            .values_list('serial', flat=True)
            .last()
        )
        attrs['serial'] = latest_serial + 1 if latest_serial is not None else 0

        # validate and assign the project
        project_id = self.context.get('project')
        try:
            attrs['project'] = Meta.projects.get(id=project_id)
        except ObjectDoesNotExist as ode:
            msg = 'Project with the given ID does not exist'
            raise serializers.ValidationError(msg) from ode

        # validate and assign the test
        test_id = self.context.get('test')
        try:
            attrs['test'] = Test.objects.get(id=test_id)
        except ObjectDoesNotExist as ode:
            msg = 'Test with the given ID does not exist'
            raise serializers.ValidationError(msg) from ode

        return attrs

    def get_or_create(self, validated_data):
        meta_serializer = serialize(MetaSerializer, validated_data.pop('meta'))
        meta, created = meta_serializer.get_or_create()
        if created:
            categorize_meta(meta)

        serial = validated_data.pop('serial')

        return MetaTest.objects.get_or_create(
            **validated_data,
            meta=meta,
            defaults={'serial': serial},
        )

    def update(self, instance, validated_data):
        meta_serializer = serialize(MetaSerializer, validated_data.pop('meta'))
        meta, created = meta_serializer.get_or_create()
        if created:
            categorize_meta(meta)

        updated_comment, created = MetaTest.objects.get_or_create(
            meta=meta,
            test=instance.test,
            project=instance.project,
            defaults={'serial': instance.serial},
        )

        if created:
            instance.delete()

        return updated_comment
