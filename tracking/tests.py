from django.test import TestCase

from .validators import is_valid_container_number


class ContainerNumberTests(TestCase):
    def test_known_valid_number(self):
        self.assertTrue(is_valid_container_number('CSQU3054383'))

    def test_wrong_check_digit_is_invalid(self):
        self.assertFalse(is_valid_container_number('CSQU3054384'))

    def test_too_short_string_is_invalid(self):
        self.assertFalse(is_valid_container_number('CSQU30'))
